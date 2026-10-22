// media-harness-projects-access task 2.3 (REQUIRED).
//
// Emits the Search_Policy builder's output so CI can validate it in Cedar STRICT schema-validation
// mode (see test_search_policy_strict.py). A statement that `FAIL_ON_ANY_FINDINGS` would reject at
// `CreatePolicy` then fails the build rather than the deploy.
//
// Writes infra/test/fixtures/search-policy.generated.json:
//   searchToolName, gatewayArn   fixed, so the Python schema can key its single action on the name
//   cases[]  {env, ingestBucket, m2cProxyBucket, policy}  the builder output for each static env's
//            real bucket names plus a fixed sample pair
//   regression  {env, form, policy}  the equivalent `!(context.input has search_image_uri) || …`
//            form the design rejects — the Python test asserts strict validation FAILS on it
//
// Run: `npm run test:emit-search-policy` (or `node infra/test/emit-search-policy.mjs`).
//
// The builder is TypeScript and imports `aws-cdk-lib`, so it is transpiled with esbuild (an infra
// devDependency) into a temp ESM module written INSIDE infra/test, so the bundle's external
// `aws-cdk-lib` import resolves against infra/node_modules.

import * as esbuild from 'esbuild';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import { writeFileSync, rmSync, mkdirSync } from 'node:fs';

const here = dirname(fileURLToPath(import.meta.url));
const builderTs = join(here, '..', 'lib', 'projects', 'search-policy.ts');

async function loadBuilder() {
  const res = await esbuild.build({
    entryPoints: [builderTs],
    bundle: true,
    format: 'esm',
    platform: 'node',
    write: false,
    external: ['aws-cdk-lib'],
    logLevel: 'silent',
  });
  const tmp = join(here, `.search-policy.import.${process.pid}.mjs`);
  writeFileSync(tmp, res.outputFiles[0].text);
  try {
    const mod = await import(pathToFileURL(tmp).href);
    return { buildSearchPolicy: mod.buildSearchPolicy, cleanup: () => rmSync(tmp, { force: true }) };
  } catch (e) {
    rmSync(tmp, { force: true });
    throw e;
  }
}

// Fixed action + resource so the Python strict schema can declare exactly this action.
const SEARCH_TOOL_NAME = 'mediaworks-harness-m2c-search___hybrid-search___search_content';
const GATEWAY_ARN = 'arn:aws:bedrock-agentcore:us-west-2:000000000000:gateway/mediaworks-harness-search-gateway';

// Each static env's real bucket names. The Ingest bucket is
// `mediaworks-harness-skills-store-<account>-us-west-2` (agent-infra-stack.ts `skillsBucketName`);
// the proxy bucket is `media.proxyBucket` from infra/lib/env-config.ts. Plus a fixed sample pair.
const BUCKET_SETS = [
  { env: 'staging', ingestBucket: 'mediaworks-harness-skills-store-100000000002-us-west-2', m2cProxyBucket: 'm2cv6-0e0000000002-100000000002-us-west-2-proxy' },
  { env: 'prod', ingestBucket: 'mediaworks-harness-skills-store-100000000003-us-west-2', m2cProxyBucket: 'm2cv6-0e0000000003-100000000003-us-west-2-proxy' },
  { env: 'sandbox', ingestBucket: 'mediaworks-harness-skills-store-100000000001-us-west-2', m2cProxyBucket: 'mediaworks-proxy-100000000001-us-west-2' },
  { env: 'sample', ingestBucket: 'harness-ingest-sample', m2cProxyBucket: 'm2c-proxy-sample' },
];

async function main() {
  const { buildSearchPolicy, cleanup } = await loadBuilder();
  try {
    const cases = BUCKET_SETS.map(({ env, ingestBucket, m2cProxyBucket }) => ({
      env,
      ingestBucket,
      m2cProxyBucket,
      policy: buildSearchPolicy({ searchToolName: SEARCH_TOOL_NAME, gatewayArn: GATEWAY_ARN, ingestBucket, m2cProxyBucket, envName: env }),
    }));

    // Regression: the equivalent negated-disjunction form, derived from the first case by replacing
    // the guarded `(if … has … then (…) else true)` image clause. Strict validation must reject it.
    const s = cases[0];
    const guarded = /\(if context\.input has search_image_s3uri[\s\S]*?else true\)/;
    const orForm =
      '(!(context.input has search_image_s3uri) ||\n' +
      `   context.input.search_image_s3uri like "s3://${s.ingestBucket}/*" ||\n` +
      `   context.input.search_image_s3uri like "s3://${s.m2cProxyBucket}/*")`;
    const regressionPolicy = s.policy.replace(guarded, orForm);
    if (regressionPolicy === s.policy || !regressionPolicy.includes('!(')) {
      throw new Error('failed to derive the regression (negated-disjunction) form from the builder output');
    }

    // Why the permit must name the spelling the upstream declares (and SearchTargetSync refuses an upstream
    // declaring the pre-rename search_image_uri): a permit naming ANOTHER spelling still validates against the
    // schema. The Python test asserts it, and that it then leaves the declared input unconstrained.
    const otherName = s.policy.replaceAll('search_image_s3uri', 'search_image_uri');
    if (otherName === s.policy) throw new Error('failed to derive the other-name form from the builder output');

    const fixture = {
      searchToolName: SEARCH_TOOL_NAME,
      gatewayArn: GATEWAY_ARN,
      cases,
      otherName: { env: s.env, policy: otherName },
      regression: { env: s.env, form: 'negated-disjunction', policy: regressionPolicy },
    };

    const outDir = join(here, 'fixtures');
    mkdirSync(outDir, { recursive: true });
    const outPath = join(outDir, 'search-policy.generated.json');
    writeFileSync(outPath, JSON.stringify(fixture, null, 2) + '\n');
    console.log(`emit-search-policy: wrote ${cases.length} statements (+2 regression forms) to ${outPath}`);
  } finally {
    cleanup();
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
