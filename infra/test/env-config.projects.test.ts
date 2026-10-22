/**
 * env-config `projects` validation tests (media-harness-projects-access task 8.1).
 * Run: `npx ts-node test/env-config.projects.test.ts` (node:assert, no framework).
 */
import * as assert from "node:assert/strict";
import * as cdk from "aws-cdk-lib";
import { resolveEnvConfig, validateProjectsConfig, EnvConfig } from "../lib/env-config";

function cfgFor(env: string): EnvConfig {
  return resolveEnvConfig(new cdk.App({ context: { env } }));
}

// --- Which envs enable Projects ---
for (const env of ["sandbox", "staging", "prod", "customer"]) {
  const cfg = cfgFor(env);
  assert.ok(cfg.projects?.enabled, `${env}: projects must be enabled`);
  assert.ok(
    (cfg.projects!.signingKeys ?? []).filter((k) => !k.retired).length === 1,
    `${env}: exactly one non-retired signing key`,
  );
}
for (const env of ["dev"]) {
  assert.equal(cfgFor(env).projects, undefined, `${env}: projects must be absent`);
}

// --- lifetime range ---
for (const bad of [59, 3601, 0, -1, 100.5]) {
  assert.throws(
    () =>
      validateProjectsConfig({
        env: "staging",
        projects: { enabled: true, signingKeys: [{ id: "k1" }], tokenLifetimeSeconds: bad },
      } as EnvConfig),
    /tokenLifetimeSeconds/,
    `lifetime ${bad} must be rejected`,
  );
}
for (const ok of [60, 3600, 1800]) {
  assert.doesNotThrow(
    () =>
      validateProjectsConfig({
        env: "staging",
        projects: { enabled: true, signingKeys: [{ id: "k1" }], tokenLifetimeSeconds: ok },
      } as EnvConfig),
    `lifetime ${ok} must be accepted`,
  );
}

// --- exactly one non-retired key required when enabled ---
assert.throws(
  () =>
    validateProjectsConfig({
      env: "staging",
      projects: { enabled: true, signingKeys: [{ id: "a", retired: true }] },
    } as EnvConfig),
  /non-retired/,
  "all-retired signing keys must be rejected",
);

console.log("env-config.projects: all assertions passed");
