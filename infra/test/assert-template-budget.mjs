#!/usr/bin/env node
/**
 * Template-size guard (media-harness-projects-access, design Component 9 "Template budget"; task 8.11).
 *
 * Walks `cdk.out` — including nested-stack templates (`*.nested.template.json`) — and fails at
 * >= 1,000,000 bytes, CloudFormation's hard template-size limit, naming the env, stack, and size. Wire
 * this into `.gitlab-ci.yml` AFTER `cdk synth` and BEFORE `cdk deploy`, so a template that would be
 * rejected at create/update time fails the build instead.
 *
 * Usage:
 *   node infra/test/assert-template-budget.mjs            # scans ./cdk.out (or $CDK_OUT)
 *   node infra/test/assert-template-budget.mjs path/to/cdk.out
 *
 * It only scans whatever `cdk synth` already produced. To cover sandbox, staging, prod, and source,
 * synth each first (the CI job synths all four, then runs this once).
 */

import { readdirSync, statSync, readFileSync } from "node:fs";
import { join, basename } from "node:path";

const LIMIT = 1_000_000;

const outDir = process.argv[2] || process.env.CDK_OUT || join(process.cwd(), "cdk.out");

/** A CloudFormation template file: `*.template.json` (parent) or `*.nested.template.json` (nested). */
function isTemplate(name) {
  return name.endsWith(".template.json");
}

/** Best-effort env + stack label from a template filename. */
function label(name) {
  const file = basename(name);
  // Parent: mediaworks-harness-infra-<env>.template.json
  const parent = file.match(/^(.*infra-([a-z]+))\.template\.json$/);
  if (parent) return { env: parent[2], stack: parent[1], nested: false };
  // Parent dev stack: mediaworks-harness-infra.template.json
  const parentDev = file.match(/^(.*infra)\.template\.json$/);
  if (parentDev) return { env: "dev", stack: parentDev[1], nested: false };
  // Nested: <ParentStackId><NestedId><hash>.nested.template.json
  const envGuess = (file.match(/infra([a-z]+)/) || [])[1] || "?";
  return { env: envGuess, stack: file.replace(/\.nested\.template\.json$/, ""), nested: true };
}

let scanned = 0;
const failures = [];
let maxEntry = { size: 0 };

let entries;
try {
  entries = readdirSync(outDir);
} catch (err) {
  console.error(`assert-template-budget: cannot read ${outDir}: ${err.message}`);
  console.error("Run `cdk synth -c env=<env>` first.");
  process.exit(2);
}

for (const name of entries) {
  const full = join(outDir, name);
  if (!isTemplate(name)) continue;
  if (!statSync(full).isFile()) continue;
  const size = statSync(full).size;
  const { env, stack, nested } = label(name);
  scanned += 1;
  if (size > maxEntry.size) maxEntry = { size, env, stack, nested };
  const flag = size >= LIMIT ? "FAIL" : "ok";
  console.log(
    `[${flag}] ${env.padEnd(8)} ${nested ? "nested" : "parent"}  ${String(size).padStart(9)} bytes  ${stack}`,
  );
  if (size >= LIMIT) {
    failures.push(`env=${env} stack=${stack} (${nested ? "nested" : "parent"}) is ${size} bytes >= ${LIMIT}`);
  }
}

if (scanned === 0) {
  console.error(`assert-template-budget: no *.template.json files in ${outDir}. Run cdk synth first.`);
  process.exit(2);
}

console.log(
  `\nassert-template-budget: scanned ${scanned} template(s); largest ${maxEntry.size} bytes ` +
    `(${maxEntry.env}, ${maxEntry.nested ? "nested" : "parent"}).`,
);

if (failures.length > 0) {
  console.error(`\nassert-template-budget FAILED (limit ${LIMIT} bytes):`);
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}

console.log("assert-template-budget: all templates under the 1,000,000-byte limit.");
