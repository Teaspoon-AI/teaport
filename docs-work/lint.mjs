#!/usr/bin/env node
// Style lint for src/content/docs. Enforces the mechanical rules in docs-work/STYLE.md.
//   node docs-work/lint.mjs                 lint every page
//   node docs-work/lint.mjs PATH [PATH...]  lint only these pages
// ERROR lines fail the run (exit 1). WARN lines are for a human or an agent to judge:
// fix them, or leave them only when the rule's exception applies.
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const ROOT = new URL('..', import.meta.url).pathname;
const DOCS = join(ROOT, 'src/content/docs');

// [regex, level, message]. Matched against prose only: frontmatter, code blocks,
// inline code, HTML comments, and link targets are stripped first.
const WORDS = [
  [/\bplease\b/i, 'ERROR', 'delete "please"'],
  [/\b(simply|obviously|of course)\b/i, 'ERROR', 'delete it'],
  [/\beas(y|ily)\b/i, 'WARN', '"easy" — what is easy for you is not easy for every reader'],
  [/\bjust\b/i, 'WARN', '"just" — usually filler; delete it or say "only"'],
  [/\be\.g\.|\bi\.e\./i, 'ERROR', 'write "for example" / "that is"'],
  [/\betc\.?(?=\s|$|[),;])/i, 'ERROR', 'rewrite without "etc."'],
  [/\bvia\b/i, 'ERROR', 'write "through", "by using", or "with"'],
  [/\bclick on\b/i, 'ERROR', 'write "click"'],
  [/\b(allows|enables) you to\b/i, 'ERROR', 'write "lets you"'],
  [/\b(utilize|leverage)s?\b/i, 'ERROR', 'write "use"'],
  [/\bin order to\b/i, 'ERROR', 'write "to"'],
  [/\b(whitelist|blacklist|master|slave|sanity[- ]check|dummy)\b/i, 'ERROR', 'see the word list in STYLE.md §3'],
  [/\b(abort|kill)(s|ed|ing)?\b/i, 'WARN', 'write "stop" / "end" unless it is a literal command or signal'],
  [/\bfile name\b/i, 'ERROR', 'write "filename"'],
  [/\bback[- ]end\b/i, 'ERROR', 'write "backend"'],
  [/\blog ?in to\b|\blogin to\b/i, 'ERROR', 'write "sign in to"'],
  [/\bonce\b/i, 'WARN', '"once" — if it means "after", write "after"'],
  [/\bsince\b/i, 'WARN', '"since" — if it means "because", write "because"'],
  [/\bmay\b/i, 'ERROR', 'write "can" (ability/permission) or "might" (possibility)'],
  [/\bwill\b/i, 'WARN', '"will" — use present tense unless the event is later in time'],
  [/\bwould\b/i, 'WARN', '"would" — write the conditional in present tense'],
  [/\b(we|we're|we've|our|us|let's)\b/i, 'WARN', 'write to "you", not "we" (Troubleshooting may report what was seen)'],
  [/\b(above|below)\b/i, 'WARN', 'do not point at content with "above"/"below"'],
  [/\(optional\)/i, 'ERROR', 'write "Optional:" at the start of the step or heading'],
  [/\bthe following command\b/i, 'WARN', 'say what the command does instead'],
  [/\b(coming soon|future release|will be added|public docs pass|pre-launch scaffold)\b/i, 'ERROR', 'do not pre-announce; describe what exists'],
  [/\b(box|appliance)\b/i, 'WARN', 'the term is "device" (STYLE.md §4)'],
  [/\b(config UI|config page|settings page)\b/i, 'ERROR', 'the term is "configuration page"'],
  [/\b(Teaport|TeaPort|Teagram|teagram)\b/, 'ERROR', 'the product is "teaport", lowercase'],
  [/\bknob\b/i, 'ERROR', 'the term is "setting"'],
  [/!/, 'WARN', 'no exclamation marks'],
  [/^\s*(\*\*)?Note:/, 'ERROR', 'use a :::note aside, not a "Note:" prefix'],
  [/\[(here|click here|this page|this link|link)\]\(/i, 'ERROR', 'use descriptive link text'],
  [/\b\d{1,2}\/\d{1,2}\/\d{2,4}\b/, 'ERROR', 'write dates as "October 27, 2026"'],
  [/\b\d+(GB|MB|KB|W|ms)\b/, 'ERROR', 'put a space between a number and its unit'],
];

// Lowercase words that may stay capitalized inside a sentence-case heading.
const PROPER = /^(teaport|Jetson|Orin|Nano|NX|AGX|JetPack|NVIDIA|CUDA|OpenClaw|NemoClaw|Pipecat|Discord|ReSpeaker|XVF3800|Linux|Ubuntu|SSH|SIP|LLM|STT|TTS|AEC|API|URL|USB|GPU|CPU|RAM|JSON|HTTP|HTTPS|Wi-Fi|OLED|I2C|ALSA|L4T|SDK|ISO|OpenRouter|Groq|Cerebras|OpenAI|Node\.js|MAXN|SUPER|FAQ|CLI|IP|DNS|VAD|G2P|Kokoro|Voxtral|Dev|Kit|I|systemd)$/;

const files = process.argv.slice(2).length
  ? process.argv.slice(2)
  : walk(DOCS).filter((f) => /\.mdx?$/.test(f));

let errors = 0, warns = 0;
for (const file of files) lint(file);
console.log(`\n${errors} error(s), ${warns} warning(s) in ${files.length} file(s)`);
process.exit(errors ? 1 : 0);

function walk(dir) {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

function report(file, line, level, msg, text = '') {
  level === 'ERROR' ? errors++ : warns++;
  const where = `${relative(ROOT, file)}:${line}`;
  console.log(`${level.padEnd(5)} ${where}  ${msg}${text ? `  ← ${text.trim().slice(0, 70)}` : ''}`);
}

function lint(file) {
  const src = readFileSync(file, 'utf8');
  const lines = src.split('\n');
  const isIndex = file.endsWith('index.mdx');

  // Frontmatter.
  let i = 0;
  if (lines[0] === '---') {
    const end = lines.indexOf('---', 1);
    const fm = lines.slice(1, end).join('\n');
    const title = fm.match(/^title:\s*(.+)$/m)?.[1]?.replace(/^['"]|['"]$/g, '');
    const desc = fm.match(/^description:\s*(.+)$/m)?.[1]?.replace(/^['"]|['"]$/g, '');
    if (!title) report(file, 1, 'ERROR', 'frontmatter has no title');
    else if (!isIndex) checkCase(file, 1, title, 'title');
    if (!desc) report(file, 1, 'ERROR', 'frontmatter has no description');
    else {
      if (!/\.$/.test(desc)) report(file, 1, 'ERROR', 'description must end with a period');
      if (desc.length > 160) report(file, 1, 'WARN', `description is ${desc.length} chars (keep under 160)`);
    }
    i = end + 1;
  } else report(file, 1, 'ERROR', 'no frontmatter');

  // Source comments must carry the pinned commit.
  for (const m of src.matchAll(/<!--\s*source:([^>]*)-->/g)) {
    if (!/@[0-9a-f]{7,}/.test(m[1])) {
      const ln = src.slice(0, m.index).split('\n').length;
      report(file, ln, 'ERROR', 'source comment has no @commit', m[0]);
    }
  }
  if (/\/etc\/teaport|:7861|:8000|install\.sh|TEAPORT_[A-Z_]+/.test(src) && !/<!--\s*source:/.test(src) && !isIndex) {
    report(file, 1, 'WARN', 'page states paths/ports/settings but has no <!-- source: --> comment');
  }

  let fence = null, lastLevel = 1, inComment = false;
  const seen = new Set();
  for (; i < lines.length; i++) {
    const raw = lines[i], n = i + 1;
    const f = raw.match(/^\s*(```+|~~~+)(.*)$/);
    if (f) {
      if (!fence) {
        fence = f[1];
        if (!f[2].trim()) report(file, n, 'ERROR', 'code block has no language (bash, ini, json, text)');
        const next = lines[i + 1] || '';
        if (/^\s*\$ /.test(next)) report(file, n + 1, 'ERROR', 'no shell prompt ($) in command blocks');
      } else if (f[1].startsWith(fence[0]) && !f[2].trim()) fence = null;
      continue;
    }
    if (fence) continue;

    // Strip HTML comments (possibly multi-line), inline code, link targets, and HTML/JSX tags.
    let text = raw;
    if (inComment) {
      if (!text.includes('-->')) continue;
      text = text.slice(text.indexOf('-->') + 3); inComment = false;
    }
    text = text.replace(/<!--.*?-->/g, '');
    if (text.includes('<!--')) { text = text.slice(0, text.indexOf('<!--')); inComment = true; }
    text = text.replace(/`[^`]*`/g, '``').replace(/\]\([^)]*\)/g, ']').replace(/<[^>]+>/g, '');
    if (/^\s*(import |:::)/.test(text)) {
      const t = text.match(/^:::\w+\[(.*)\]/);
      if (t) checkWords(file, n, t[1]);
      continue;
    }

    const h = raw.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const level = h[1].length, heading = h[2].trim();
      if (level === 1) report(file, n, 'ERROR', 'no H1 in the body; the frontmatter title is the H1');
      if (level > lastLevel + 1) report(file, n, 'ERROR', `heading skips a level (H${lastLevel} → H${level})`);
      lastLevel = level;
      if (/\]\(/.test(heading)) report(file, n, 'ERROR', 'no links in headings');
      if (/[.:;]$/.test(heading)) report(file, n, 'ERROR', 'no end punctuation in headings');
      if (/^\d+[.)]\s/.test(heading)) report(file, n, 'ERROR', 'do not number headings');
      if (/^Step \d/i.test(heading)) report(file, n, 'ERROR', 'do not number headings');
      if (/^[A-Z][a-z]+ing\b/.test(heading) && !/^(Troubleshooting|Updating|Billing|Pricing)\b/.test(heading))
        report(file, n, 'WARN', 'task headings start with a bare verb ("Install", not "Installing")', heading);
      const key = heading.toLowerCase();
      if (seen.has(key)) report(file, n, 'ERROR', 'duplicate heading on this page', heading);
      seen.add(key);
      checkCase(file, n, heading.replace(/`[^`]*`/g, ''), 'heading');
    }
    checkWords(file, n, text);
  }
}

function checkWords(file, n, text) {
  for (const [re, level, msg] of WORDS) if (re.test(text)) report(file, n, level, msg, text);
}

function checkCase(file, n, text, what) {
  const words = text.replace(/[^\w\s.\-/]/g, ' ').split(/\s+/).filter(Boolean).slice(1);
  const bad = words.filter((w) => /^[A-Z][a-z]/.test(w) && !PROPER.test(w));
  if (bad.length) report(file, n, 'WARN', `${what} should be sentence case (${bad.join(', ')})`, text);
}
