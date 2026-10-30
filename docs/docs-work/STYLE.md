# teaport docs style guide

This is the style for every page under `src/content/docs/`. It condenses the
[Google developer documentation style guide](https://developers.google.com/style) to the
rules that matter for this site, adds teaport's own terms, and maps both onto Starlight
markup. When this file and the Google guide disagree, this file wins. When this file is
silent, follow the Google guide.

`node docs-work/lint.mjs` checks most of the mechanical rules. A clean lint is required. It
is not enough by itself: the lint cannot tell whether a sentence is true.

---

## 1. Accuracy rules (these come before style)

1. **Every fact comes from the pinned source.** Ports, paths, defaults, environment
   variables, flags, command names, file names, unit names, and version numbers must be
   read from the product repo at the pinned commit (see `PROMPT.md` → *Source*). Never
   write one from memory, from an old docs page, or from a guess.
2. **Cite it.** Put a source comment right after the paragraph, list, or table that states
   the fact. Use the file path, the line range, and the pinned commit:

   ```markdown
   <!-- source: install.sh:375-386 @1e5604c -->
   ```

   One comment can list several sources, separated by `;`. The comment is invisible on the
   rendered page.
3. **If you can't verify it, don't write it.** Leave it out and record it under *Open
   questions* in your report. Don't soften a guess with "probably" or "usually".
4. **Describe what exists now.** Don't pre-announce features. Don't write "coming soon",
   "will be added", "in a future release", or "the public docs pass". Remove the old
   *Pre-launch scaffold* notes. If something is genuinely unavailable, say so in the
   present tense: "The hosted engine artifacts aren't published yet."
5. **Keep what was learned the hard way.** The Troubleshooting and Installation pages record
   failures seen on real boards. Keep each one unless the pinned source shows the failure
   can no longer happen. If you remove one, say why in your report.

---

## 2. Voice and tone

- Write to the reader as **you**. Don't write "we", "our", or "let's". Exception: the
  Troubleshooting page can say "This failure was seen on a Jetson Orin Nano" (passive is fine
  there because the actor doesn't matter).
- Be conversational, direct, and calm. No jokes, no exclamation marks, no hype ("powerful",
  "seamless", "blazing").
- Write for a global audience: short sentences, one idea each, no idioms, no culture-specific
  references. Use standard American spelling.
- Use **active voice**. Make the actor clear.
  - Yes: "The installer writes `/etc/teaport/brain.env`."
  - No: "`/etc/teaport/brain.env` is written."
- Use **present tense**. Use *will* only for something that happens later in time.
  - Yes: "The brain restarts and reconnects to the engine."
  - No: "The brain will restart and will reconnect to the engine."
  - Avoid the hypothetical *would*: "If you set `X`, the brain does Y."
- Use contractions where they read naturally: *don't*, *isn't*, *can't*, *you're*. Don't
  use ambiguous or rare ones (*there'd*, *it'll*, *should've*).
- Put the **condition before the instruction**.
  - Yes: "If the brain doesn't start, run `teaport doctor`."
  - No: "Run `teaport doctor` if the brain doesn't start."
- Put the **goal before the action**.
  - Yes: "To check the services, run `teaport status`."
  - No: "Run `teaport status` to check the services."
- Use *can* for ability or permission, *might* for possibility. Don't use *may*.

---

## 3. Word list

Replace the left column with the right column. The lint catches most of these.

| Don't write | Write |
|---|---|
| please, please note | (delete it) |
| simply, just, easy, easily, obviously, of course | (delete it) — what is easy for you isn't easy for every reader |
| e.g. | for example |
| i.e. | that is |
| etc. | rewrite: "such as A, B, and C" or "including" |
| via | through, by using, with |
| click on | click |
| hit (a key) | press |
| allows you to, enables you to | lets you |
| utilize, leverage | use |
| once (meaning *after*) | after |
| since (meaning *because*) | because |
| in order to | to |
| abort, kill (a process) | stop, end, cancel (keep `kill` only as a literal command or signal name) |
| master / slave | primary / replica, controller |
| whitelist / blacklist | allowlist / denylist |
| sanity check | check, verification |
| dummy | placeholder, sample |
| file name | filename |
| back end, back-end | backend |
| log in, login (as a verb) | sign in |
| set up (verb) / setup (noun) | keep this distinction: "Set up the line." "The setup takes 30 minutes." |
| the following command (as the only introduction) | say what the command does: "To restart the brain, run this command:" |
| above, below (to point at content) | earlier, later, the following, or a link |
| (optional) | `Optional:` at the start of the step or heading |

---

## 4. teaport terminology

Use these terms exactly. Once a term is chosen, never switch to a synonym on the same site.

| Term | Use it for | Don't use |
|---|---|---|
| teaport | The product. Always lowercase in prose, even at the start of a sentence. Try not to start a sentence with it. | Teaport, TeaPort, Teagram |
| device | The Jetson that runs teaport. | box, appliance, unit, board (except "dev kit" in the hardware name) |
| Jetson Orin Nano Dev Kit (8 GB) | The reference hardware, first mention on a page. Then "the Jetson". | Nano 8GB, the Orin |
| engine | The speech engine service (`teaport-engine`). It does speech-to-text and text-to-speech. | STT server, model server |
| brain | The voice pipeline service (`teaport-brain`). | bot, backend, server |
| plugin | The OpenClaw realtime-voice provider. | extension, add-on |
| agent | OpenClaw or NemoClaw, whichever gives the brain its tools. | assistant (for the agent), gateway (except for the literal OpenClaw gateway) |
| voice-only mode | A device with no agent (`TEAPORT_AGENT=none`). | headless, standalone |
| Talk session | A conversation over the brain's `/talk` WebSocket. | talk call, chat |
| phone call | A call over the Session Initiation Protocol (SIP) line. | SIP session, telephony session |
| configuration page | The brain's web page at `/config`. | config UI, settings page, dashboard |
| setting | One configurable value, such as `LLM_MODEL`. | knob, variable, option (except a command-line option) |
| secret | A credential file in `~/.config/teaport/`. | token file, key file |
| installer | `install.sh`, including the one-line `curl` form. | install script, setup script |
| large language model (LLM) | The model the brain sends text to. | AI, model (alone, when ambiguous) |

**Abbreviations.** Spell out an abbreviation the first time it appears on each page, with
the short form in parentheses: "speech-to-text (STT)", "Session Initiation Protocol
(SIP)", "acoustic echo cancellation (AEC)". After that, use the short form. Don't spell out
abbreviations every reader knows: API, CPU, GPU, HTTP, JSON, RAM, SSH, URL, USB. Don't use an
abbreviation as a verb: "connect with SSH", not "ssh into the device".

---

## 5. Page types

Every page is one of four types. The type decides the headings and the structure. Don't mix
two types on one page; link to the other page instead.

### Task page (how-to): Installation, Prepare the Jetson, the guides

- Title: a bare-infinitive verb phrase in sentence case: "Set up phone calls", "Update
  teaport". Not "Setting up phone calls", not "Phone Calls".
- Open with one or two sentences: what the reader gets when they finish.
- Then **Before you begin**: a bulleted list of what the reader needs (hardware, access,
  accounts, earlier pages). Prerequisites never go in a note.
- Then one H2 per procedure. Each H2 is a task heading: "Configure the SIP line".
- End with **What's next**: two to four links.

### Concept page: Architecture, FAQ

- Title and headings: noun phrases. "Architecture", "One conversation at a time", "Network
  ports". Not "Understanding the architecture".
- Explain how something works and why. Ordinary paragraphs. Diagrams as text tables or
  lists are fine; don't add images.
- No procedures on concept pages. Link to the task page.

### Reference page: CLI, Settings, Installer options

- Title: a noun phrase. "teaport CLI reference", "Settings reference".
- Consistent structure for every entry: name, synopsis, description, options, example.
- Use tables for settings and options. Use description lists (a bolded term, then its
  definition) when a table cell would need more than one sentence.
- Exhaustive and dry. Every entry the source has, no entry it doesn't.

### Troubleshooting page

- Each H2 or H3 is the **symptom as the reader sees it**: "The installer stops
  immediately", "The speech sounds robotic". Not the cause.
- Under each symptom: **Cause** (one short paragraph), then **Fix** (a procedure).
- Start the page with a short *Start here* procedure (`teaport status`, `teaport doctor`,
  `teaport logs`).

---

## 6. Procedures

- Introduce a procedure with a complete sentence ending in a colon:
  "To configure the line, follow these steps:". Not "To configure the line:" alone.
- Use a **numbered list** for two or more steps. Use a **single bullet** for a one-step
  procedure, not a numbered list of one.
- Start each step with an imperative verb: "Run", "Open", "Edit", "Restart".
- One action per step. If a step needs a decision, split it.
- Location and goal come first in the step: "On the Jetson, run this command:" /
  "To apply the change, restart the brain:".
- Order inside a step: the action, then the command block, then the placeholders, then
  the output, then the result in its own sentence.
- Put the result after the action in the same step: "Run `teaport status`. All three
  services show `active`." Don't make the result its own step.
- Mark optional steps with `Optional:` at the start of the step.
- Sub-steps use lowercase letters. In Markdown, indent them under the step as a nested
  ordered list; Starlight renders the numbering.
- Give **one** way to do each task — the shortest one that works. If there are two real
  paths (for example, the configuration page or editing the file over SSH), put each under
  its own H3.
- Don't repeat a procedure that exists on another page. Link to it.
- Don't write "please". Don't include keyboard shortcuts unless the step needs a keypress
  (then: "press **Enter**").

Example:

~~~markdown
## Restart the brain

To apply a change to `/etc/teaport/brain.env`, restart the brain:

1. On the Jetson, run this command:

   ```bash
   sudo systemctl restart teaport-brain
   ```

   A live Talk session ends. A live phone call stays connected and resumes when the brain
   is back.
2. Check that the brain is running:

   ```bash
   teaport status
   ```

   The `teaport-brain` line shows `active`.
~~~

---

## 7. Code, commands, and placeholders

- Put these in `code font`: commands, flags, file and directory paths, setting names,
  service and unit names, ports, URLs that the reader types, environment variables, and
  literal values (`true`, `0`, `none`).
- Don't put product names in code font: teaport, OpenClaw, NemoClaw, Pipecat.
- Use a fenced code block for every command the reader runs, with a language:
  `bash` for shell, `ini` for env files and `.conf` files, `json` for JSON, `text` for output.
- **No shell prompt** (`$`) in command blocks. Starlight adds a copy button; a prompt breaks
  the copied command. Put input and output in **separate** blocks.
- Introduce output with: "The output is similar to the following:". Show output only when
  the reader must check or copy something from it. Mark omitted lines with `...` on its own
  line.
- Placeholders: UPPER_SNAKE_CASE, no brackets in commands the reader copies. Explain each
  placeholder right after the block:

  ~~~markdown
  ```bash
  ssh USER@DEVICE_IP
  ```

  Replace the following:

  - `USER`: your user name on the Jetson.
  - `DEVICE_IP`: the IP address of the Jetson.
  ~~~

- In a synopsis (CLI reference only), show optional arguments in `[brackets]` and
  mutually exclusive choices as `{a|b}`.
- Break a command longer than 80 characters with ` \` and indent the continuation lines by
  four spaces.
- Use `sudo` only where the command needs it. Check the source.

---

## 8. Formatting and structure

- **Headings:** sentence case. Task headings start with a bare verb ("Install the
  plugin"). Concept headings are noun phrases ("Network ports"). No end punctuation. No
  links in headings. Avoid code in headings; if you must, add a noun: "The
  `teaport sip` command". Don't skip a level (H2 then H4). Make headings unique within the
  page. Don't number headings ("1. Flash JetPack"); the order shows the sequence.
- **No H1 in the body.** Starlight renders the frontmatter `title` as the H1. Start the body
  at H2.
- **Frontmatter:** every page has `title` (sentence case) and `description` (one sentence,
  ends with a period, under 160 characters).
- **Lists:** numbered for sequences, bulleted for everything else. Introduce every list with
  a complete sentence. Make items parallel (all sentences, or all fragments). Use the serial
  comma: "the engine, the brain, and the plugin".
- **Tables:** introduce with a sentence. Sentence-case headers. No empty cells — write
  "None" or "—".
- **UI elements** in **bold**, with the label exactly as the UI shows it: "Click **Save**."
- **Links:** descriptive text, never "click here" or "this page". Internal links are
  root-relative with a trailing slash: `[Settings reference](/reference/settings/)`. Link to
  a section with its anchor: `/guides/troubleshooting/#the-speech-sounds-robotic`.
- **Dates:** unambiguous: "October 27, 2026". Never `10/27/26`.
- **Numbers:** numerals for measurements and anything technical (`8 GB`, `30 minutes`,
  port `7861`). Put a space between a number and its unit: `8 GB`, `25 W`.
- Don't use *above* / *below* to point at content. Don't use "Note:" as a paragraph prefix;
  use an aside (next section).

---

## 9. Notices (Starlight asides)

Draft the content as normal text first. Use an aside only if it meets all three tests: the
reader doesn't need it to finish the task, it doesn't send them elsewhere, and it isn't part
of the flow.

| Google notice | Starlight syntax | Use it for |
|---|---|---|
| Note | `:::note` | A useful aside. |
| — | `:::tip` | A faster or better way. Rarely needed. |
| Caution | `:::caution` | Proceed carefully; something can go wrong. |
| Warning | `:::danger` | Data loss, an action you can't undo, or a security risk. |

- Use few asides. Never put two next to each other.
- Never put a prerequisite, a required step, or an expected result in an aside.
- Give an aside a custom title only when it helps: `:::caution[The ISO image is missing two power modes]`.

---

## 10. Starlight and Markdown mechanics

- Pages are `.md` unless they import components. Only `index.mdx` uses components. Don't
  convert a `.md` page to `.mdx`.
- Don't edit `astro.config.mjs`, `package.json`, or anything outside your packet's files.
  The *integrate* packet owns the sidebar.
- Don't add images. Don't add new npm packages.
- After you edit, the site must still build: `npx astro build`. Fix any error your page
  causes.
