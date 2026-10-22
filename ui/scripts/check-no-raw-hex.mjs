#!/usr/bin/env node
/**
 * Fail if a chrome component hardcodes a colour.
 *
 * The app carried ~500 raw hex values, which is why dark mode looked
 * half-finished: those values could not flip, so dark text landed on dark
 * grounds and white panels punched holes in the pane. They are now tokens, and
 * this keeps them that way — the failure mode is silent and only shows up in the
 * theme nobody was looking at, so a reviewer will not reliably catch it.
 *
 * Allowed, deliberately:
 *   - `components/nle/**`, `components/renderer/**` — speaker identity colours,
 *     waveforms, syntax highlighting. These are CONTENT and must not flip.
 * A white label on a filled button used to be exempt. It no longer is: with a
 * mint primary in dark-kirocrew, white on the fill is ~1.5:1, so those labels
 * must read `theme.colors.onPrimary` and flip with the theme.
 *   - `styles/tokens.ts`, `styles/global.css` — where colour is defined.
 *   - tests, which may pin a rendered value.
 *
 * Run: npm run lint:colors
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

const SRC = path.join(process.cwd(), 'src');
const EXEMPT_DIR = [
  path.join('components', 'nle'),
  path.join('components', 'renderer'),
];
const EXEMPT_FILE = [
  path.join('styles', 'tokens.ts'),
  path.join('styles', 'global.css'),   // where colour is defined
  path.join('views', 'LandingPage.css'), // pre-login brand surface: fixed by design, not themed
];

/** `var(--token, #fallback)` is correct usage — the token leads, the hex is the
 *  fallback if it is ever missing. Only a BARE hex is a finding. */
const VAR_FALLBACK = /var\(\s*--[\w-]+\s*,\s*$/;

/** Replace comment bodies with spaces, preserving newlines so line numbers hold. */
function blankComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, c => c.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:])\/\/[^\n]*/g, (c, lead) => lead + ' '.repeat(c.length - lead.length));
}

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    // CSS is scanned too: markdown.css hardcoded a light inline-code chip that
    // stayed light in every theme, and a .ts-only sweep could never see it.
    else if (/\.(tsx?|css)$/.test(entry)) out.push(full);
  }
  return out;
}

const offenders = [];
for (const file of walk(SRC)) {
  const rel = path.relative(SRC, file);
  if (/\.test\.tsx?$/.test(rel)) continue;
  if (EXEMPT_DIR.some(d => rel.startsWith(d))) continue;
  if (EXEMPT_FILE.includes(rel)) continue;

  const isCss = rel.endsWith('.css');
  /*
   * Bare CSS colour keywords, not just hex. `color: 'white'` on a filled button
   * is the same defect as `color: '#fff'` — it cannot flip — but a hex-only
   * check could not see it, which is how six of them survived the migration and
   * shipped a white-on-mint filter chip.
   */
  const KEYWORD = /\b(?:color|background|backgroundColor|borderColor|fill|stroke)\s*:\s*(['"]?)(white|black)\1\s*[,;}]/g;
  // Comments are blanked (newlines kept, so reported line numbers stay right):
  // a note explaining which colour was replaced is documentation, not a finding.
  const lines = blankComments(readFileSync(file, 'utf8')).split('\n');
  lines.forEach((line, i) => {
    /*
     * One regex for both languages. It used to require the hex to be the WHOLE
     * quoted string in TS (`/(['"])#[0-9a-f]{3,8}\1/`), which made every hex
     * inside a longer value invisible — `border: '1px solid #e0e0e0'` and
     * `borderBottom: "1px solid #f0f0f0"` passed the check while being exactly
     * the defect it exists to catch. 99 of them had accumulated behind it.
     */
    const re = /#[0-9a-fA-F]{3,8}\b/g;
    let m;
    while ((m = re.exec(line))) {
      if (VAR_FALLBACK.test(line.slice(0, m.index))) continue;
      offenders.push(`${rel}:${i + 1}  ${m[0]}  ${line.trim().slice(0, 90)}`);
    }
    KEYWORD.lastIndex = 0;
    let k;
    while ((k = KEYWORD.exec(line))) {
      offenders.push(`${rel}:${i + 1}  ${k[2]}  ${line.trim().slice(0, 90)}`);
    }
  });
}

if (offenders.length) {
  console.error(`\n${offenders.length} hardcoded colour(s) in chrome — use a token from styles/tokens.ts:\n`);
  for (const o of offenders) console.error('  ' + o);
  console.error('\nIf the colour is content (a speaker identity, a waveform, syntax), put it under');
  console.error('components/nle or components/renderer, or add a narrow exemption here.\n');
  process.exit(1);
}
console.log('no raw chrome colours ✓');
