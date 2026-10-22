#!/usr/bin/env node
/**
 * Ratchet on the style-token rules that `check-no-raw-hex.mjs` cannot express.
 *
 * That script is a HARD ZERO gate: no raw hex, no bare `white`/`black`. It sits at
 * zero and must stay there. The rules here cannot start at zero — there are
 * hundreds of existing violations — so this is a declining budget instead: every
 * file gets its current count as a baseline, exceeding it fails, and dropping
 * below it prompts you to lower the baseline. New files start at zero implicitly.
 *
 * Why these four rules specifically — each is a defect that is invisible in review
 * and only shows up in a theme or a setting nobody was looking at:
 *
 *   px-font-size    `--font-size-base` cascades from `html`, so Settings → General
 *                   → Font Size scales only text with NO explicit size. A fixed
 *                   `fontSize: 13` opts that element out. With most of the UI
 *                   opted out, Small/Large distort proportions rather than
 *                   rescaling — labels grow while their values do not.
 *
 *   raw-colour-fn   `rgba()`/`hsl()` cannot flip with the theme. The existing ones
 *                   are also off-palette: Tailwind amber/red/blue in the Tools and
 *                   Workflow editors, legacy Fluent blue/green/red in ActivityPanel.
 *                   In dark-kirocrew, where success is mint, an rgba() success pill
 *                   renders in old Fluent green beside mint status dots.
 *                   Use `tint(theme.colors.success)` — it mixes against
 *                   `--color-surface`, so one declaration is right in all themes.
 *
 *   px-radius       `theme.radius` is panel(8)/button(6)/badge(3). The most common
 *                   literal in the app is `4`, which is not any of them, which is
 *                   why two button families drifted apart (radius 6 vs radius 4).
 *
 *   raw-font-family A literal stack bypasses `--font-body`/`--font-mono`, so the
 *                   element renders in a different typeface from the app. There
 *                   were six competing mono stacks. Legitimate exception: CSS built
 *                   for a FOREIGN document, where a `var()` cannot resolve — see
 *                   `renderer/SandboxFrame.tsx`.
 *
 * Run:  npm run lint:styles
 * Ratchet down after a cleanup:  npm run lint:styles -- --update
 */
import { readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const SRC = path.join(process.cwd(), 'src');
const BASELINE = path.join(process.cwd(), 'scripts', 'style-baseline.json');

/** Where colour IS defined, and the pre-login brand surface (fixed by design). */
const COLOUR_DEFINITION_FILES = [
  path.join('styles', 'tokens.ts'),
  path.join('styles', 'global.css'),
  path.join('views', 'LandingPage.css'),
];

/**
 * Media rendering is exempt from the COLOUR rule only: speaker identity colours,
 * waveforms and syntax highlighting are CONTENT and must not flip with the theme.
 * They are NOT exempt from the size/radius/font rules, which are chrome concerns.
 */
const CONTENT_COLOUR_DIRS = [
  path.join('components', 'nle'),
  path.join('components', 'renderer'),
];

const RULES = [
  {
    id: 'px-font-size',
    test: line => [
      ...line.matchAll(/fontSize:\s*[0-9.]+/g),
      ...line.matchAll(/font-size:\s*[0-9.]+px/g),
    ],
    fix: 'use var(--fs-2xs|xs|sm|md|lg|xl|2xl) or theme.typography.sizes.*',
  },
  {
    id: 'raw-colour-fn',
    test: line => [...line.matchAll(/\b(?:rgba?|hsla?)\(/g)],
    fix: 'use a theme.colors.* token, or tint(theme.colors.x) for a soft fill',
    skip: rel => CONTENT_COLOUR_DIRS.some(d => rel.startsWith(d)),
  },
  {
    id: 'px-radius',
    test: line => [
      ...line.matchAll(/borderRadius:\s*[0-9]+/g),
      ...line.matchAll(/border-radius:\s*[0-9.]+px/g),
    ],
    fix: 'use theme.radius.panel | .button | .badge',
    /*
     * `config/branding.ts` is DATA, not chrome: a per-brand logo corner radius,
     * runtime-configurable from `/config`. A brand's mark is square, rounded or
     * circular because of that brand, not because of our button geometry, so it
     * must not follow `theme.radius`.
     */
    skip: rel => rel === path.join('config', 'branding.ts'),
  },
  {
    id: 'raw-font-family',
    /*
     * `var(...)` and `inherit` are the CORRECT forms and must not be flagged —
     * the first version of this rule did, which punished exactly the code the
     * sweep had just fixed. Only a literal family stack is a finding.
     */
    test: line => [
      ...line.matchAll(/fontFamily:\s*['"](?!var\(|inherit)/g),
      ...line.matchAll(/font-family:\s*(?!var\()['"a-zA-Z-]/g),
    ],
    fix: "use var(--font-body) / var(--font-mono); literals only for foreign-document CSS",
  },
];

/**
 * Replace comment bodies with spaces, preserving newlines so line numbers hold.
 * (Duplicated from check-no-raw-hex.mjs — two small lint scripts sharing a
 * six-line helper is not worth a module.)
 */
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
    else if (/\.(tsx?|css)$/.test(entry)) out.push(full);
  }
  return out;
}

/** @returns {Map<ruleId, Map<relPath, {count, samples}>>} */
function scan() {
  const found = new Map(RULES.map(r => [r.id, new Map()]));
  for (const file of walk(SRC)) {
    const rel = path.relative(SRC, file);
    if (/\.test\.tsx?$/.test(rel)) continue;
    if (COLOUR_DEFINITION_FILES.includes(rel) && rel.endsWith('.css')) continue;

    const lines = blankComments(readFileSync(file, 'utf8')).split('\n');
    for (const rule of RULES) {
      if (rule.skip?.(rel)) continue;
      // tokens.ts legitimately holds the var() references themselves.
      if (rule.id === 'raw-font-family' && rel === path.join('styles', 'tokens.ts')) continue;
      if (rule.id === 'raw-colour-fn' && COLOUR_DEFINITION_FILES.includes(rel)) continue;

      let count = 0;
      const samples = [];
      lines.forEach((line, i) => {
        for (const m of rule.test(line)) {
          count++;
          if (samples.length < 3) samples.push(`${rel}:${i + 1}  ${line.trim().slice(0, 84)}`);
        }
      });
      if (count) found.get(rule.id).set(rel, { count, samples });
    }
  }
  return found;
}

const found = scan();
const updating = process.argv.includes('--update');

let baseline = {};
try {
  baseline = JSON.parse(readFileSync(BASELINE, 'utf8'));
} catch {
  if (!updating) {
    console.error(`\nNo baseline at scripts/style-baseline.json.`);
    console.error(`Create it from the current tree:  npm run lint:styles -- --update\n`);
    process.exit(1);
  }
}

if (updating) {
  const next = {};
  for (const rule of RULES) {
    next[rule.id] = Object.fromEntries(
      [...found.get(rule.id)].map(([rel, v]) => [rel, v.count]).sort((a, b) => a[0].localeCompare(b[0])),
    );
  }
  writeFileSync(BASELINE, JSON.stringify(next, null, 2) + '\n');
  const total = RULES.reduce((n, r) => n + Object.values(next[r.id]).reduce((a, b) => a + b, 0), 0);
  console.log(`baseline written — ${total} violation(s) across ${RULES.length} rules`);
  for (const rule of RULES) {
    const t = Object.values(next[rule.id]).reduce((a, b) => a + b, 0);
    console.log(`  ${rule.id.padEnd(16)} ${t}`);
  }
  process.exit(0);
}

const regressions = [];
const improvements = [];

for (const rule of RULES) {
  const allowed = baseline[rule.id] ?? {};
  const current = found.get(rule.id);

  for (const [rel, { count, samples }] of current) {
    const budget = allowed[rel] ?? 0;
    if (count > budget) {
      regressions.push(
        `${rule.id}: ${rel} has ${count}, budget ${budget}\n` +
          samples.map(s => `      ${s}`).join('\n') +
          `\n      → ${rule.fix}`,
      );
    } else if (count < budget) {
      improvements.push(`${rule.id}: ${rel} ${budget} → ${count}`);
    }
  }
  for (const rel of Object.keys(allowed)) {
    if (!current.has(rel) && allowed[rel] > 0) improvements.push(`${rule.id}: ${rel} ${allowed[rel]} → 0`);
  }
}

if (regressions.length) {
  console.error(`\n${regressions.length} style-token regression(s) — these are theme/settings defects:\n`);
  for (const r of regressions) console.error('  ' + r + '\n');
  console.error('The budget only shrinks. Fix the new violations, or if you deliberately');
  console.error('added one (foreign-document CSS, say), say why in a comment and run');
  console.error('`npm run lint:styles -- --update`.\n');
  process.exit(1);
}

const remaining = RULES.reduce(
  (n, r) => n + Object.values(baseline[r.id] ?? {}).reduce((a, b) => a + b, 0),
  0,
);

if (improvements.length) {
  console.log(`style ratchet: ${improvements.length} file(s) improved — lower the baseline:\n`);
  for (const i of improvements) console.log('  ' + i);
  console.log('\n  npm run lint:styles -- --update\n');
} else {
  console.log(`style ratchet ✓  (${remaining} grandfathered violation(s) remaining)`);
}
