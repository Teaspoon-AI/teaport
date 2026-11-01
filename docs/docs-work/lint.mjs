#!/usr/bin/env node
// Style lint for src/content/docs. Enforces the mechanical rules in docs-work/STYLE.md.
//   node docs-work/lint.mjs                 lint every page
//   node docs-work/lint.mjs PATH [PATH...]  lint only these pages
//   node docs-work/lint.mjs --fix-sources [PATH...]
//                                           rewrite source comments to the current line ranges
//                                           and hashes, then lint (see "Source comments" below)
// ERROR lines fail the run (exit 1). WARN lines are for a human or an agent to judge:
// fix them, or leave them only when the rule's exception applies.
import { readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { join, relative } from 'node:path';

const ROOT = new URL('..', import.meta.url).pathname;
const DOCS = join(ROOT, 'src/content/docs');
// Source comments cite files by path from the repository root; docs/ is a subdirectory of it.
const REPO = execFileSync('git', ['rev-parse', '--show-toplevel'], { cwd: ROOT, encoding: 'utf8' }).trim();

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

// Source comments. A comment cites the product files a page states facts from:
//   <!-- source: install.sh:375-386#3f9a2c1b; brain/x.py:1-4,9#aa11bb22 -->
// Items are separated by "; ". Each is path:ranges#HASH, with the path relative to the repository
// root and ranges that are comma-separated N or N-M (1-based, inclusive). HASH is the first 8 hex
// characters of the SHA-256 of the cited lines, taken in the order written and joined with "\n".
// A mismatch means the cited lines changed: re-check the page's claim, then run --fix-sources
// or update the range. In .mdx pages the comment is {/* source: ... */}.
const SOURCE_RE = /(<!--|\{\/\*)\s*source:([\s\S]*?)(-->|\*\/\})/g;
const ITEM_RE = /^(?:([^\s:#;@]+):)?([\d,-]+)(?:#([0-9a-f]{8}))?(?:\s*@([0-9a-f]{7,40}))?$/;
// Docs that moved to this site; their claims have to be re-cited from the product sources.
const DEAD_FILE_RE = /^docs\/(CONFIG|FAQ|INSTALL|ARCHITECTURE)\.md$/;

const fileCache = new Map();

const args = process.argv.slice(2);
const FIX_SOURCES = args.includes('--fix-sources');
const paths = args.filter((a) => !a.startsWith('--'));
const files = paths.length ? paths : walk(DOCS).filter((f) => /\.mdx?$/.test(f));

let errors = 0, warns = 0;
if (FIX_SOURCES) fixSources(files);
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

  checkSources(file, src);
  if (/\/etc\/teaport|:7861|:8000|install\.sh|TEAPORT_[A-Z_]+/.test(src) && !/(<!--|\{\/\*)\s*source:/.test(src) && !isIndex) {
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

function repoLines(path) {
  if (!fileCache.has(path)) {
    let lines = null;
    try {
      if (statSync(join(REPO, path)).isFile()) lines = toLines(readFileSync(join(REPO, path), 'utf8'));
    } catch {}
    fileCache.set(path, lines);
  }
  return fileCache.get(path);
}

function toLines(text) {
  const lines = text.split('\n');
  if (text.endsWith('\n')) lines.pop();
  return lines;
}

function git(...a) {
  return execFileSync('git', a, { cwd: REPO, encoding: 'utf8', maxBuffer: 1 << 28, stdio: ['ignore', 'pipe', 'ignore'] });
}

// "1-4,9" -> [[1, 4], [9, 9]]; null when it is not that form.
function parseRanges(s) {
  const out = [];
  for (const part of s.split(',')) {
    const m = part.match(/^(\d+)(?:-(\d+))?$/);
    if (!m) return null;
    const a = +m[1], b = m[2] ? +m[2] : a;
    if (a < 1 || b < a) return null;
    out.push([a, b]);
  }
  return out;
}

function fmtRanges(ranges) {
  return ranges.map(([a, b]) => (a === b ? `${a}` : `${a}-${b}`)).join(',');
}

function hashLines(lines, ranges) {
  const cited = ranges.flatMap(([a, b]) => lines.slice(a - 1, b));
  return createHash('sha256').update(cited.join('\n'), 'utf8').digest('hex').slice(0, 8);
}

// Split a comment body into items. An old-form item can take its commit from the end of the
// comment, and "cli/teaport:85;95-96" continues the previous path.
function parseItems(body) {
  const pieces = body.split(';').map((p) => p.trim()).filter(Boolean);
  const lastCommit = body.match(/@([0-9a-f]{7,40})(?![\s\S]*@[0-9a-f]{7,40})/)?.[1] ?? null;
  let prevPath = null;
  return pieces.map((raw) => {
    const m = raw.match(ITEM_RE);
    if (!m) return { raw, kind: 'bad' };
    const path = m[1] ?? prevPath;
    if (m[1]) prevPath = m[1];
    const item = { raw, path, ranges: parseRanges(m[2]), hash: m[3] ?? null, commit: m[4] ?? null, continued: !m[1] };
    if (!path || !item.ranges) return { raw, kind: 'bad' };
    if (!item.hash && !item.commit && lastCommit) item.commit = lastCommit;
    item.kind = item.hash ? 'new' : item.commit ? 'old' : 'nohash';
    return item;
  });
}

function itemText(item) {
  const cite = `${item.path}:${fmtRanges(item.ranges)}`;
  if (item.hash) return `${cite}#${item.hash}`;
  return item.commit ? `${cite} @${item.commit}` : cite;
}

function checkSources(file, src) {
  for (const m of src.matchAll(SOURCE_RE)) {
    const ln = src.slice(0, m.index).split('\n').length;
    const items = parseItems(m[2]);
    if (!items.length) report(file, ln, 'ERROR', 'source comment has no items', m[0]);
    for (const item of items) {
      if (item.kind === 'bad' || item.kind === 'old') {
        const hint = item.kind === 'old' || /@/.test(item.raw) ? ' (old @commit form: run --fix-sources)' : '';
        report(file, ln, 'ERROR', `unparseable source item, want path:ranges#HASH${hint}`, item.raw);
        continue;
      }
      const lines = repoLines(item.path);
      if (!lines) { report(file, ln, 'ERROR', 'cited file does not exist', item.raw); continue; }
      const past = item.ranges.find(([, b]) => b > lines.length);
      if (past) { report(file, ln, 'ERROR', `range ${fmtRanges([past])} is out of bounds (${item.path} has ${lines.length} lines)`, item.raw); continue; }
      if (item.kind === 'nohash') { report(file, ln, 'ERROR', 'source item has no #HASH (run --fix-sources, or add the hash)', item.raw); continue; }
      const got = hashLines(lines, item.ranges);
      if (got !== item.hash)
        report(file, ln, 'ERROR', `cited lines changed (hash ${item.hash}, now ${got}) — re-check the page's claim, then --fix-sources or update the range`, item.raw);
    }
  }
}

// Map old line numbers (at COMMIT) to working-tree line numbers through git's own diff.
// Returns a function old -> new, or null for a line the diff rewrote or removed.
function lineMap(commit, path) {
  let diff;
  try { diff = git('diff', '-U0', '--no-color', '--no-ext-diff', commit, '--', path); } catch { return () => null; }
  const hunks = [...diff.matchAll(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/gm)].map((h) => ({
    a: +h[1], b: h[2] === undefined ? 1 : +h[2], d: h[4] === undefined ? 1 : +h[4],
  }));
  return (old) => {
    let shift = 0;
    for (const { a, b, d } of hunks) {
      const last = b ? a + b - 1 : a; // for b = 0, new lines are inserted after line a
      if (b && old >= a && old <= last) return null;
      if (last < old) shift += d - b;
    }
    return old + shift;
  };
}

// Where do the lines that RANGES cite in the old text of PATH (at COMMIT) live now?
// Each block must be found once as a contiguous block; when a block occurs more than once, git's
// diff decides. Returns { ranges } or { why }.
function relocate(path, ranges, commit, oldLines) {
  const cur = repoLines(path);
  if (!cur) return { why: 'file no longer exists' };
  if (!oldLines) {
    try { oldLines = toLines(git('show', `${commit}:${path}`)); } catch { return { why: `file does not exist at ${commit}` }; }
  }
  let map;
  const out = [];
  for (const [a, b] of ranges) {
    if (b > oldLines.length) return { why: `range ${a}-${b} is out of bounds at ${commit}` };
    const block = oldLines.slice(a - 1, b), hits = [];
    for (let i = 0; i + block.length <= cur.length; i++) if (block.every((l, k) => cur[i + k] === l)) hits.push(i + 1);
    if (hits.length === 1) { out.push([hits[0], hits[0] + block.length - 1]); continue; }
    if (!hits.length) return { why: 'cited lines are not in the file any more' };
    map ??= lineMap(commit, path);
    const start = map(a);
    if (start !== null && hits.includes(start) && map(b) === start + block.length - 1) out.push([start, start + block.length - 1]);
    else return { why: `cited lines occur ${hits.length} times in the file` };
  }
  return { ranges: out };
}

// For a new-form item whose hash no longer matches, find the newest commit whose version of the
// file has the cited lines with that hash, then relocate from there.
function relocateByHash(item) {
  let commits;
  try { commits = git('log', '--format=%H', '-n', '300', '--', item.path).split('\n').filter(Boolean); } catch { return { why: 'no history' }; }
  for (const commit of commits) {
    let old;
    try { old = toLines(git('show', `${commit}:${item.path}`)); } catch { continue; }
    if (item.ranges.some(([, b]) => b > old.length)) continue;
    if (hashLines(old, item.ranges) === item.hash) return relocate(item.path, item.ranges, commit, old);
  }
  return { why: 'no version of the file in history has this hash' };
}

// --fix-sources: rewrite each old-form item (path:ranges @commit) and each stale new-form item
// to the current ranges and hash. Items that cannot be placed stay as they were and are listed.
function fixSources(list) {
  const done = { migrated: 0, shifted: 0 };
  const drifted = [], dead = [], stale = [];
  for (const file of list) {
    const src = readFileSync(file, 'utf8');
    const out = src.replace(SOURCE_RE, (all, open, body, close, at) => {
      const ln = src.slice(0, at).split('\n').length;
      const where = `${relative(ROOT, file)}:${ln}`;
      const items = parseItems(body);
      if (!items.length || items.some((i) => i.kind === 'bad')) {
        for (const i of items) if (i.kind === 'bad') drifted.push({ where, text: i.raw, why: 'unparseable' });
        return all;
      }
      let changed = false;
      const next = items.map((item) => {
        if (item.kind === 'nohash') return itemText(item);
        const lines = repoLines(item.path);
        if (item.kind === 'new') {
          if (lines && !item.ranges.some(([, b]) => b > lines.length) && hashLines(lines, item.ranges) === item.hash) return itemText(item);
        } else if (DEAD_FILE_RE.test(item.path) && !lines) {
          dead.push({ where, text: item.raw });
          return itemText(item);
        }
        const res = item.kind === 'old' ? relocate(item.path, item.ranges, item.commit) : relocateByHash(item);
        if (!res.ranges) {
          (item.kind === 'old' ? drifted : stale).push({ where, text: itemText(item), why: res.why });
          return itemText(item);
        }
        changed = true;
        item.kind === 'old' && fmtRanges(res.ranges) === fmtRanges(item.ranges) ? done.migrated++ : done.shifted++;
        return `${item.path}:${fmtRanges(res.ranges)}#${hashLines(lines, res.ranges)}`;
      });
      return changed ? `${open} source: ${next.join('; ')} ${close}` : all;
    });
    if (out !== src) writeFileSync(file, out);
  }
  const show = (title, rows) => {
    if (!rows.length) return;
    console.log(`\n${title} (${rows.length})`);
    for (const r of rows) console.log(`  ${r.where}  ${r.text}${r.why ? `  — ${r.why}` : ''}`);
  };
  console.log(`--fix-sources: ${done.migrated} migrated in place, ${done.shifted} migrated with a new range or relocated`);
  show('Drifted: left as they were, check the page against the current code', drifted);
  show('Stale: hash does not match and no earlier version of the file was found', stale);
  show('Dead: cite a deleted docs/*.md file; re-cite the product source by hand', dead);
}

function checkWords(file, n, text) {
  for (const [re, level, msg] of WORDS) if (re.test(text)) report(file, n, level, msg, text);
}

function checkCase(file, n, text, what) {
  const words = text.replace(/[^\w\s.\-/]/g, ' ').split(/\s+/).filter(Boolean).slice(1);
  const bad = words.filter((w) => /^[A-Z][a-z]/.test(w) && !PROPER.test(w));
  if (bad.length) report(file, n, 'WARN', `${what} should be sentence case (${bad.join(', ')})`, text);
}
