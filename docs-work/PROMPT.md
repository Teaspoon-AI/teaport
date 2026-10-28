# teaport docs: agent prompt

You are a technical writer working on the teaport documentation site. teaport is a
local voice assistant for the NVIDIA Jetson Orin. This repo
(`~/Desktop/teaport-docs`) is an Astro Starlight site, and it is the canonical user
documentation. The product code lives in another repo. The pages here describe the product
as it was in August 2026. Since then the product has changed by more than 300 commits, and
several pages were never finished.

The operator assigns you **one packet** (for example, `PACKET=phone-calls`). Do that packet
and nothing else.

## Read these first, in this order

1. `docs-work/STYLE.md`: read all of it. Every rule applies to you.
2. Your packet in the *Packets* section of this file.
3. The current version of your page, if it exists.
4. The sources your packet lists, read at the pinned commit (see *Source*).

`AGENTS.md` is also loaded for you. Its *Pipecat context hub* section doesn't apply to
documentation work. Ignore it unless your packet says otherwise.

## Source

The product repo is at `~/Desktop/teaport`. **Read only the pinned commit. Never read its
working tree:** the checkout is behind, has untracked files, and has many worktrees under
`.claude/worktrees/` that contain experimental code. Never read anything under
`.claude/worktrees/`.

```bash
PIN=1e5604c                     # origin/main, October 27, 2026
R=~/Desktop/teaport
git -C $R show "${PIN}:install.sh" | sed -n '370,390p'        # read lines 370-390
git -C $R grep -n 'LLM_BASE_URL' "${PIN}" -- install.sh cli/   # search, with line numbers
git -C $R ls-tree -r --name-only "${PIN}" -- systemd/          # list files
```

Always write `"${PIN}:path"` with braces and quotes. In zsh, `$PIN:path` is parsed as a
modifier and fails.

Where the facts live:

| Fact | Source of truth |
|---|---|
| Every setting: name, default, file, which service reads it, help text | `brain/teaport_brain/config_schema.toml` |
| Generated settings tables (output of the schema) | `docs/CONFIG.md` (the part after the `<!-- generated from ... -->` marker) |
| Installer steps, flags, environment overrides, paths | `install.sh` (the header comment at lines 1-60 lists the flags and overrides) |
| CLI commands and options | `cli/teaport` (the header comment lists the commands) |
| Services, ports, and users | `systemd/*.service.in` |
| Release versions and downloads | `manifest/stable.json`, `manifest/beta.json` |
| Product overview and status | `README.md` |

The product's `docs/*.md` and `*/README.md` files are useful **drafts**: they explain
features at length. They are written in a different style and can be out of date. Use
them to understand a feature. Then verify each fact you keep against the files in the
table, and cite the file you verified it in.

## Rules

1. **Write only your packet's files**, plus your report at `docs-work/reports/PACKET.md`.
   Don't edit `astro.config.mjs`, other pages, `STYLE.md`, `PROMPT.md`, or `lint.mjs`.
2. **The product repo is read-only.** Don't edit, commit, check out, pull, or run anything
   in it, except the read commands in *Source*.
3. **Don't touch hardware.** Don't connect to a Jetson with SSH. Don't run `install.sh` or
   `teaport`.
4. **Don't commit.** Several agents work in this tree at the same time. The operator
   reviews and commits.
5. **Don't invent.** If the source doesn't say it, don't write it. Put it under *Open
   questions* in your report. A short, correct page is better than a long, wrong one.
6. **Cite every fact** with a `<!-- source: FILE:LINES @1e5604c -->` comment
   (STYLE.md §1). Re-verify each source comment already on the page. Their line numbers
   are from an older commit and are almost certainly wrong now.
7. **Link, don't repeat.** If another packet's page owns a topic, write one sentence and
   link to that page. The packet list shows which page owns which topic. Links to pages
   that don't exist yet are fine. The *integrate* packet checks them.
8. **Don't add** images, components, npm packages, or `.mdx` pages.

## Work steps

1. Read the files in *Read these first*.
2. Make a list of the facts the page must state. For each fact, find it at the pinned
   commit and write down the file and line numbers.
3. Write the page. Follow the page-type template in STYLE.md §5.
4. Run the style lint on your page and fix every `ERROR`:

   ```bash
   node docs-work/lint.mjs src/content/docs/PATH/TO/PAGE.md
   ```

   Read every `WARN`. Fix it, unless the rule's stated exception applies to that line.
5. Build the site and fix any error your page causes:

   ```bash
   npx astro build 2>&1 | tail -20
   ```

6. Reread the page one last time and check it against STYLE.md §2, §3, and §4. The lint
   doesn't catch passive voice, a wrong term, or an untrue sentence.
7. Write your report (next section). Then stop.

## Report

Write `docs-work/reports/PACKET.md` in this format:

```markdown
# PACKET

## Changed files
- src/content/docs/...

## What changed
Three to eight bullets. What you added, removed, or corrected, and why.

## Removed content
Every fact or section that you removed from the old page, and the source that shows it is
no longer true. Write "None" if you removed nothing.

## Open questions
Every fact that you couldn't verify and left out. Every place where the sources
contradict each other. Give the file and line numbers for each one.

## Lint
The last line of the lint output, and any WARN that you left, with the reason.
```

---

## Packets

Each packet names its files, its page type (STYLE.md §5), what it covers, what it must
link to instead of covering, and where to start reading. All paths in *Read* are in the
product repo at `PIN`.

The packets in **Wave 1** don't depend on each other. Run them in parallel. Run
**Wave 2** after every Wave 1 packet is finished.

### Wave 1

#### `settings`: Settings reference (new)

- File: `src/content/docs/reference/settings.md`
- Type: reference. Title: "Settings reference".
- Covers every setting in `config_schema.toml`: the files where settings live and how to
  apply a change (the `[stores.*]` tables), then one section per settings group, then
  *Settings that constrain each other*.
- Method: take the generated tables from `docs/CONFIG.md` (the generator's own output)
  and keep their rows **exactly as generated**. Don't reword `help` text, even where it
  breaks STYLE.md: the generator owns it. List the help-text style problems that you see in
  your report under *Open questions*, so they can be fixed in the schema. You write the
  page introduction, the H2 headings (sentence case), and one or two sentences that
  introduce each table.
- Keep the generated marker comment and add a source comment that cites
  `brain/teaport_brain/config_schema.toml @1e5604c`.
- Leave out the prose sections of `docs/CONFIG.md` that are procedures (SIP setup,
  turn-taking per front-end, one engine one conversation). Other packets own them.
- Read: `brain/teaport_brain/config_schema.toml`, `docs/CONFIG.md`.

#### `configuration`: Configure teaport (rewrite)

- File: `src/content/docs/guides/configuration.md` (keep the path)
- Type: task. Title: "Configure teaport".
- Covers: how to change a setting with the configuration page (`http://DEVICE_IP:7861/config`,
  how to open it with the gateway token, what it shows about services still running old
  values); how to change a setting by editing the file over SSH; where secrets go and why
  an environment variable that is set overrides its secret file; how to apply a change
  (which service to restart, and what a restart interrupts); how to set up the LLM (base
  URL, key, model) as the most common example.
- Links to: [Settings reference](/reference/settings/) for every individual setting. Don't
  repeat the settings tables here.
- Read: `docs/CONFIG.md` (prose before the tables), `brain/teaport_brain/config_ui.py`,
  `brain/teaport_brain/config_apply.py`, `brain/teaport_brain/settings.py`,
  the `[stores.*]` tables in `config_schema.toml`, and the secrets handling in `install.sh`.

#### `installation`: Install teaport (update)

- File: `src/content/docs/getting-started/installation.md` (keep the path)
- Type: task. Title: "Install teaport".
- Keep the structure and the measured facts from the current page. Update each fact
  that changed: JetPack and L4T versions, disk space, the agent choice, the installer
  prompts, the self-test, and the engine release. End the page with a procedure that checks
  the installation (`teaport status`, `teaport doctor`) and starts a first conversation.
  Write that procedure only if the source shows how. Otherwise, add it to *Open questions*.
- Mention the opt-in components (phone calls, Discord bridge, local audio) in one sentence
  each, with a link. Don't document them here.
- Links to: `/getting-started/prepare-the-jetson/`, `/guides/configuration/`,
  `/reference/installer/`, `/guides/troubleshooting/`.
- Read: `install.sh`, `install-bootstrap.sh`, `README.md`, `docs/INSTALL.md`,
  `manifest/stable.json`.

#### `prepare`: Prepare the Jetson (update)

- File: `src/content/docs/getting-started/prepare-the-jetson.md` (keep the path)
- Type: task. Title: "Prepare the Jetson".
- Restyle to STYLE.md: remove numbered headings, and write to the reader as "you". Check each
  command and number against the source. `install.sh` sets up swap and other parts of the
  device. Find what it does and don't tell the reader to do it again by hand.
- Read: `install.sh` (search for `swap`, `nvpmodel`, `multi-user`, `zram`), `docs/INSTALL.md`.

#### `phone-calls`: Set up phone calls (new)

- File: `src/content/docs/guides/phone-calls.md`
- Type: task. Title: "Set up phone calls".
- Covers: what phone calls need (a SIP trunk or session border controller (SBC) account,
  the separate SIP gateway); `teaport sip configure` (interactive and `--conf`);
  `teaport sip status`, `aec`, `restart`, and `disable`; how a call that arrives during a
  Talk session behaves (ring, ask, hold, resume); how to test the phone path without a
  line.
- Links to: `/reference/cli/#...` for the full option list, `/reference/settings/` for the
  SIP settings, `/reference/architecture/` for the session arbiter.
- Read: `cli/teaport` (`sip_*` functions), `systemd/teaport-sip.service.in`,
  `docs/CONFIG.md` (*SIP telephony (opt-in)*, *One engine, one conversation*),
  `docs/FAQ.md`, `brain/teaport_brain/sip_server.py` (top comment only),
  `tools/sip-line-guard`.

#### `discord`: Set up the Discord bridge (new)

- File: `src/content/docs/guides/discord-bridge.md`
- Type: task. Title: "Set up the Discord bridge".
- Covers: what the bridge does; how to install and turn it on (`TEAPORT_ENABLE_BRIDGE=1`,
  the guild and followed-user IDs); where the bot token goes; how to check it and turn it off.
- Read: `bridge/discord/README.md`, `bridge/discord/index.js`, `bridge/discord/env.js`,
  `systemd/teaport-discord-bridge.service.in`, `install.sh` (search `BRIDGE`), the
  *Discord bridge* group in `config_schema.toml`.

#### `local-audio`: Use a microphone on the device (new)

- File: `src/content/docs/guides/local-audio.md`
- Type: task. Title: "Use a microphone on the device".
- Covers: the supported USB microphone array (ReSpeaker XVF3800); how to turn on the local
  audio bridge (`TEAPORT_ENABLE_LOCAL_AUDIO=1`, `TEAPORT_LOCAL_AUDIO_DEVICE`); wake words
  and the sleeping microphone; the LED and busy lamp, if the source documents them for
  users; echo handling. Leave out the OLED face: it is for Teaspoon AI devices only.
- Read: `systemd/teaport-local-audio.service.in`, `brain/teaport_brain/local_audio.py`,
  `wake_gate.py`, `wake_words.py`, `xvf_led.py`, `xvf_echo.py` (top comments),
  `packaging/xvf3800/`, `install.sh` (search `LOCAL_AUDIO`), and the *Local audio bridge* and
  *Busy lamp* groups in `config_schema.toml`.

#### `wifi`: Connect the device to Wi-Fi (new)

- File: `src/content/docs/guides/wifi-setup.md`
- Type: task. Title: "Connect the device to Wi-Fi".
- Covers: how to start Wi-Fi setup (by voice, and any other way that the source shows);
  how to join the `teaport-XXXX` setup network from a phone; where the password comes from;
  what happens on success, on failure, and after the ten-minute timeout; how to cancel.
- Read: `brain/teaport_brain/wifi_setup.py` (top comment), `wifi_voice.py`, `wifi.py`,
  `systemd/teaport-wifi-setup.service.in`, `packaging/wifi-setup/`, `install.sh`
  (`phase_wifi_setup`, around line 2287), and the *Wi-Fi setup* group in
  `config_schema.toml`.

#### `updating`: Update teaport (new)

- File: `src/content/docs/guides/updating.md`
- Type: task. Title: "Update teaport".
- Covers: `teaport update --check` and `teaport update`; the release channels (stable
  and beta), if a user can choose one; how to update only the brain
  (`install.sh --only brain`); how to roll back the brain (`--rollback brain`); how many
  releases are kept; what an update restarts.
- Read: `cli/teaport` (`cmd_update`), `install.sh` (header comment, `--only`,
  `--rollback`), `manifest/*.json`, `docs/INSTALL.md` (*Updating the brain*).

#### `cli`: teaport CLI reference (update)

- File: `src/content/docs/reference/cli.md` (keep the path)
- Type: reference. Title: "teaport CLI reference".
- One H2 per command: `status`, `doctor`, `update`, `logs`, and `sip`. Under `sip`, one
  H3 per subcommand. Each entry has a synopsis, a description, the options, the exit
  behavior if the source defines it, and one example. Every option that `cli/teaport`
  parses, and nothing else.
- Read: `cli/teaport`, all of it.

#### `installer`: Installer reference (new)

- File: `src/content/docs/reference/installer.md`
- Type: reference. Title: "Installer reference".
- Covers: the two ways to run the installer (the one-line command and from a checkout);
  every flag (`--only`, `--rollback`, `--dry-run`, `--accept-eula`, `--help`); every
  environment override in the header comment, in a table with its default; the
  files and directories the installer creates (`/opt/teaport`, `/etc/teaport`,
  `~/.config/teaport`); and the services it installs.
- Read: `install.sh` (header comment, then verify each flag in the argument parser),
  `install-bootstrap.sh`, `systemd/`.

#### `architecture`: Architecture (rewrite)

- File: `src/content/docs/reference/architecture.md` (keep the path)
- Type: concept. Title: "Architecture". Use ordinary paragraphs. This page explains how
  teaport works and why.
- Covers: the engine, the brain, and the plugin, and how audio and text move between them;
  the front-ends of the brain (`/talk`, the SIP line, the Discord bridge, the local audio
  bridge); one conversation at a time (the session arbiter, and who gets the engine);
  what the voice is told; tools in agent mode and in voice-only mode; a table of network
  ports and which service listens on each; what leaves the device.
- Read: `docs/ARCHITECTURE.md`, `brain/README.md`, `plugin/README.md`, `systemd/`,
  `brain/teaport_brain/session_arbiter.py` (top comment), `brain/teaport_brain/tools.py`
  (tool names only).

### Wave 2

#### `troubleshooting`: Troubleshooting (update)

- File: `src/content/docs/guides/troubleshooting.md` (keep the path)
- Type: troubleshooting.
- For each existing section: check the cause and the fix against the pinned source. Fix
  the source comments. Keep the section if the failure can still happen. Change each section
  to the Symptom, Cause, and Fix structure.
- Add the failures that the new Wave 1 pages describe (phone calls, Wi-Fi setup, local audio,
  updates), but only the ones that `teaport doctor` or the source names explicitly. Read
  `cli/teaport` `cmd_doctor`: each check that it can fail, with the fix hint it prints, is a
  symptom candidate.
- Link to the Wave 1 pages. Don't repeat their procedures.

#### `faq`: FAQ (rewrite)

- File: `src/content/docs/guides/faq.md` (keep the path)
- Type: concept. Use an H2 for each question, written as the reader would ask it. Each
  answer is two to five sentences and ends with a link to the page that owns the topic.
- Start from `docs/FAQ.md` at the pinned commit. Check each answer against the Wave 1
  pages and the source.

#### `index`: Home page (update)

- File: `src/content/docs/index.mdx`
- Update the tagline and the cards so that each card links to a page that exists. Use
  four to six cards. Keep the existing components and the splash template.

#### `integrate`: Sidebar, links, and final checks

Run this packet last, after every other packet is finished.

1. In `astro.config.mjs`, set the sidebar to this structure. Change only the `sidebar`
   array:
   - **Get started:** Prepare the Jetson, Install teaport
   - **Guides:** Configure teaport, Set up phone calls, Set up the Discord bridge, Use a
     microphone on the device, Connect the device to Wi-Fi, Update teaport,
     Troubleshooting, FAQ
   - **Reference:** Architecture, teaport CLI reference, Installer reference, Settings
     reference

   Make the sidebar labels the same as the page titles.
2. Run `npx astro build`. It must finish with no errors and no warnings about missing
   slugs.
3. Find every internal link (`](/`) in `src/content/docs/` and check that each target
   page and anchor exists in `dist/`. Fix broken links. Change the link, not the target
   page's headings.
4. Run `node docs-work/lint.mjs`. There must be zero errors.
5. Read every report in `docs-work/reports/`. Then write
   `docs-work/reports/integrate.md`. It collects every *Open question* into one
   numbered list, grouped by page, for the operator to answer.
