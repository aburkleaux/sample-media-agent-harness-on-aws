#!/usr/bin/env node
/**
 * One-shot codemod: fixed px -> design token, for a named file.
 *
 * Deliberately NOT a blanket sweep over src/. It takes explicit paths so each
 * surface lands as its own reviewable commit, and so files whose numbers are
 * CONTENT rather than chrome (markdown type scales, `components/renderer/**`,
 * `components/nle/**`) are simply never passed to it.
 *
 * Snapping rules — off-scale values move to the nearest rung, which is a small
 * deliberate appearance change and the point of having a scale:
 *
 *   fontSize   8,9,10 -> 3xs(10)   11 -> 2xs(11)   12 -> xs(12)   13 -> sm(13)
 *              14 -> md(14)        15,16 -> lg(16) 17,18,19 -> xl(17)
 *              20,22 -> 2xl(20)    (.5 values snap down to their integer rung)
 *
 *   radius     2,3 -> badge(3)     4,5,6 -> button(4)   7..12 -> panel(8)
 *              999 -> pill         0 and '50%' left alone
 *
 * Run:  node scripts/tokenise.mjs src/components/Foo.tsx [more...]
 *       node scripts/tokenise.mjs --dry src/components/Foo.tsx
 */
import { readFileSync, writeFileSync } from 'node:fs';

const FONT = [
  [[8, 9, 10, 10.5], 'var(--fs-3xs)'],
  [[11, 11.5], 'var(--fs-2xs)'],
  [[12, 12.5], 'var(--fs-xs)'],
  [[13, 13.5], 'var(--fs-sm)'],
  [[14], 'var(--fs-md)'],
  [[15, 16], 'var(--fs-lg)'],
  [[17, 18, 19], 'var(--fs-xl)'],
  [[20, 22], 'var(--fs-2xl)'],
];
const RADIUS = [
  [[2, 3], 'theme.radius.badge'],
  [[4, 5, 6], 'theme.radius.button'],
  [[7, 8, 9, 10, 12], 'theme.radius.panel'],
  [[999], 'theme.radius.pill'],
];

const lookup = table => {
  const m = new Map();
  for (const [values, token] of table) for (const v of values) m.set(v, token);
  return m;
};
const FONT_MAP = lookup(FONT);
const RADIUS_MAP = lookup(RADIUS);

const dry = process.argv.includes('--dry');
const files = process.argv.slice(2).filter(a => !a.startsWith('--'));
if (!files.length) {
  console.error('usage: node scripts/tokenise.mjs [--dry] <file...>');
  process.exit(1);
}

let grandTotal = 0;
for (const file of files) {
  const before = readFileSync(file, 'utf8');
  const changes = [];

  let after = before.replace(/(fontSize:\s*)(\d+(?:\.\d+)?)\b/g, (whole, lead, num) => {
    const token = FONT_MAP.get(parseFloat(num));
    if (!token) { changes.push(`  ! fontSize ${num} — no rung, left alone`); return whole; }
    changes.push(`    fontSize ${num} -> ${token}`);
    return lead + `'${token}'`;
  });

  after = after.replace(/(borderRadius:\s*)(\d+)\b/g, (whole, lead, num) => {
    const token = RADIUS_MAP.get(parseInt(num, 10));
    if (!token) { changes.push(`  ! borderRadius ${num} — no rung, left alone`); return whole; }
    changes.push(`    borderRadius ${num} -> ${token}`);
    return lead + token;
  });

  const applied = changes.filter(c => !c.startsWith('  !')).length;
  grandTotal += applied;
  console.log(`${file}  (${applied} change${applied === 1 ? '' : 's'})`);
  for (const c of changes) console.log(c);

  if (!dry && after !== before) writeFileSync(file, after);
}
console.log(`\n${dry ? '[dry run] ' : ''}${grandTotal} substitution(s)`);
