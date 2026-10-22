/**
 * Feature: media-harness-projects-access, Property 8: Search_Policy decision
 *
 * Task 2.2 (REQUIRED). A Cedar DECISION property test over the ACTUAL output of the pure builder
 * `infra/lib/projects/search-policy.ts`: the statement must PERMIT a `search_content` call whose
 * `collection_id` matches the principal's `project_id` tag and whose `search_image_s3uri` (when present)
 * is inside one of the two allowed buckets, and DENY everything else — other collections, other
 * actions, other gateways, lookalike/other-scheme/out-of-bucket image URIs, and non-string inputs.
 *
 * Run: `npm run test:search-policy-decision` (ts-node + node:assert — the infra test runner; no
 * framework), with fast-check for generation (>=100 runs per property) per the plan's convention.
 *
 * Mechanism / substitution note:
 *   The design (Property-table row 8) prescribes evaluating the builder output with
 *   `@cedar-policy/cedar-wasm` in `ui/src/search-policy.property.test.ts`. We keep the prescribed
 *   REAL-engine mechanism (`@cedar-policy/cedar-wasm`, added as an infra devDependency) but run it
 *   from the infra test runner rather than ui/vitest, because the builder imports `aws-cdk-lib`,
 *   which is a dependency of `infra`, NOT of `ui` — a ui-side test importing the builder cannot
 *   resolve it, and loading the wasm engine under vite adds avoidable fragility. The engine, the
 *   builder under test, and every decision asserted here are identical to the design's; only the
 *   runner and generator library (fast-check, already an infra devDependency) differ. Strict Cedar
 *   schema-validation of the same builder output is task 2.3 (`test_search_policy_strict.py`).
 *   Reference oracle cross-checked against `docs/prototypes/media-harness-projects/validation/test_v1_cedar_policy.py`.
 */
import * as assert from 'node:assert/strict';
import * as fc from 'fast-check';
import { buildSearchPolicy } from '../lib/projects/search-policy';

// cedar-wasm ships its Node (synchronous-wasm) build under the ./nodejs subpath as CommonJS. The
// subpath types are not visible under classic module resolution, so type the slice we use locally.
interface CedarAnswer {
  readonly type: 'success' | 'failure';
  readonly response?: { readonly decision: 'allow' | 'deny' };
  readonly errors?: unknown[];
}
interface CedarWasm {
  isAuthorized(call: unknown): CedarAnswer;
}
// eslint-disable-next-line @typescript-eslint/no-var-requires
const cedar: CedarWasm = require('@cedar-policy/cedar-wasm/nodejs');

// ---------------------------------------------------------------------------------------------
// The statement under test is the builder's real output for a concrete, valid bucket pair.
// ---------------------------------------------------------------------------------------------
const TOOL = 'mediaworks-harness-m2c-search___hybrid-search___search_content';
const GW = 'arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/mediaworks-harness-search-gateway';
const INGEST = 'harness-ingest-sandbox';
const PROXY = 'm2c-proxy-sandbox';

const POLICY = buildSearchPolicy({
  searchToolName: TOOL,
  gatewayArn: GW,
  ingestBucket: INGEST,
  m2cProxyBucket: PROXY,
  envName: 'sandbox',
});

// Guard the two documented shape invariants of the builder output before trusting its decisions:
// it must use the strict-validator-safe `if … then … else` form and never the `!(… has …) ||` form.
assert.ok(
  POLICY.includes('if context.input has search_image_s3uri') && POLICY.includes('else true)'),
  'builder output must use the guarded `if … then … else true` image clause',
);
assert.ok(!POLICY.includes('!('), 'builder output must never use the negated-disjunction form');

const PID = 'p' + 'a'.repeat(32);
const OTHERPID = 'p' + 'b'.repeat(32);
const SUB = 'actor-' + 'c'.repeat(26);

type Tags = Record<string, unknown>;
type Input = Record<string, unknown>;

function decide(tags: Tags, input: Input, action: string = TOOL, resource: string = GW): 'allow' | 'deny' {
  const ans = cedar.isAuthorized({
    principal: { type: 'AgentCore::OAuthUser', id: 'u1' },
    action: { type: 'AgentCore::Action', id: action },
    resource: { type: 'AgentCore::Gateway', id: resource },
    context: { input },
    policies: { staticPolicies: POLICY },
    entities: [
      { uid: { type: 'AgentCore::OAuthUser', id: 'u1' }, attrs: {}, parents: [], tags },
      { uid: { type: 'AgentCore::Gateway', id: GW }, attrs: {}, parents: [] },
    ],
  });
  // A genuine evaluation (an erroring `like`/`==` on a non-string yields success+deny). Only an
  // ill-formed request (e.g. a JSON null attribute) fails to parse; the generators never emit one.
  assert.equal(ans.type, 'success', `unexpected request parse failure: ${JSON.stringify(ans.errors)}`);
  return ans.response!.decision;
}

/** The reference oracle: exactly the semantics Property 8 specifies for the permit. */
function expectAllow(tags: Tags, input: Input, action: string, resource: string): boolean {
  if (action !== TOOL || resource !== GW) return false;
  const pid = tags['project_id'];
  if (typeof pid !== 'string') return false;
  if (!('collection_id' in input)) return false;
  const cid = input['collection_id'];
  if (typeof cid !== 'string' || cid !== pid) return false;
  if ('search_image_s3uri' in input) {
    const uri = input['search_image_s3uri'];
    if (typeof uri !== 'string') return false;
    // Ingest bucket: a Search_Rendition only (`projects/*/rendition.jpg`, where `*` may span `/` or be empty).
    const pre = `s3://${INGEST}/projects/`, suf = '/rendition.jpg';
    const rendition = uri.startsWith(pre) && uri.endsWith(suf) && uri.length >= pre.length + suf.length;
    if (!(rendition || uri.startsWith(`s3://${PROXY}/`))) return false;
  }
  return true;
}

// =============================================================================================
// Part A — enumerated required cases (readable, deterministic; mirror the V1 prototype but run
// against the builder's own output).
// =============================================================================================
const ALLOW: Array<[Tags, Input]> = [
  [{ project_id: PID }, { collection_id: PID, search_term: 'x' }],
  [{ project_id: PID, sub: SUB }, { collection_id: PID, search_image_s3uri: `s3://${INGEST}/projects/${PID}/image:1/rendition.jpg` }],
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: `s3://${PROXY}/frames/1.jpg` }],
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: `s3://${PROXY}/` }], // `*` matches empty tail
];
const DENY: Array<[Tags, Input]> = [
  [{ project_id: PID }, { collection_id: OTHERPID }], // another Project's id
  [{ project_id: PID }, { collection_id: PID.toUpperCase() }], // case variant
  [{ project_id: PID }, { collection_id: PID + 'x' }], // suffix
  [{ project_id: PID }, { collection_id: PID.slice(1) }], // prefix / shorter
  [{ project_id: PID }, { collection_id: 'default' }],
  [{ project_id: PID }, { collection_id: '*' }],
  [{ project_id: PID }, { collection_id: '%' }],
  [{ project_id: PID }, {}], // omitted collection_id
  [{ project_id: PID }, { collection_id: '' }], // empty
  [{}, { collection_id: PID }], // no project_id tag (Cognito / machine token)
  [{ sub: PID }, { collection_id: PID }], // sub == collection is NOT enough
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: 's3://other-bucket/a.jpg' }],
  // Ingest bucket: only a Search_Rendition — an original, or any other object, is denied.
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: `s3://${INGEST}/projects/${PID}/a.jpg` }],
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: `s3://${INGEST}/projects/${PID}/image:1/shot.jpg` }],
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: `s3://${INGEST}/artifacts/s1/input/a.jpg` }],
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: `s3://${INGEST}/` }],
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: `s3://${INGEST}/projects/rendition.jpg` }],
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: `s3://${INGEST}-evil/a.jpg` }],
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: `s3://${INGEST}.evil/a.jpg` }],
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: `s3://${INGEST}` }], // bare, no `/`
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: `S3://${INGEST}/a.jpg` }], // upper scheme
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: `https://${INGEST}.s3.amazonaws.com/a.jpg` }],
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: 'file:///etc/passwd' }],
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: '' }], // present, matches neither
  [{ project_id: PID }, { collection_id: 123 }], // non-string collection_id
  [{ project_id: PID }, { collection_id: [PID] }],
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: 5 }], // non-string image uri
  [{ project_id: PID }, { collection_id: PID, search_image_s3uri: { a: 1 } }],
];

for (const [tags, input] of ALLOW) {
  assert.equal(decide(tags, input), 'allow', `expected allow: ${JSON.stringify({ tags, input })}`);
  assert.equal(expectAllow(tags, input, TOOL, GW), true, `oracle disagrees (allow): ${JSON.stringify({ tags, input })}`);
}
for (const [tags, input] of DENY) {
  assert.equal(decide(tags, input), 'deny', `expected deny: ${JSON.stringify({ tags, input })}`);
  assert.equal(expectAllow(tags, input, TOOL, GW), false, `oracle disagrees (deny): ${JSON.stringify({ tags, input })}`);
}

// Every other action denies, even with a matching collection_id.
const OTHER_ACTIONS = [
  'mediaworks-harness-m2c-search___hybrid-search___list_collections',
  'mediaworks-harness-m2c-search___hybrid-search___create_collection',
  'mediaworks-harness-m2c-search___hybrid-search___index_content',
  'mediaworks-harness-m2c-search___hybrid-search___get_job_status',
  'mediaworks-harness-m2c-search___hybrid-search___describe_tool',
  'mediaworks-harness-m2c-search___hybrid-search-admin___delete_collection',
  'mediaworks-harness-m2c-search___hybrid-search-admin___create_collection',
  'x_amz_bedrock_agentcore_search',
  'some___generated___name',
  TOOL + '-but-not-quite',
];
for (const action of OTHER_ACTIONS) {
  assert.equal(decide({ project_id: PID }, { collection_id: PID }, action), 'deny', `expected deny for action ${action}`);
}
// Any other resource denies.
for (const resource of [GW + '-2', 'arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/other-gw']) {
  assert.equal(decide({ project_id: PID }, { collection_id: PID }, TOOL, resource), 'deny', `expected deny for resource ${resource}`);
}

// =============================================================================================
// Part B — the decision property: for any generated tag set, input, action, and resource, the
// engine's decision on the builder output equals the reference oracle. (>=100 runs.)
// =============================================================================================
const collectionArb: fc.Arbitrary<{ omit: true } | { value: unknown }> = fc.oneof(
  fc.constant<{ omit: true }>({ omit: true }),
  fc.record({
    value: fc.constantFrom(PID, OTHERPID, '', PID.toUpperCase(), PID.slice(1), PID + 'x', 'default', '*', '%', SUB),
  }),
  // Non-string values (never JSON null — null is a request parse failure, not a decision).
  fc.record({ value: fc.oneof(fc.integer({ min: -5, max: 5 }), fc.array(fc.string(), { maxLength: 2 }), fc.dictionary(fc.string(), fc.string(), { maxKeys: 2 })) }),
  fc.record({ value: fc.string() }),
);
const imageArb: fc.Arbitrary<{ omit: true } | { value: unknown }> = fc.oneof(
  fc.constant<{ omit: true }>({ omit: true }),
  fc.record({
    value: fc.constantFrom(
      `s3://${INGEST}/projects/${PID}/a.jpg`,
      `s3://${INGEST}/projects/${PID}/image:1/rendition.jpg`,
      `s3://${INGEST}/projects//rendition.jpg`,
      `s3://${INGEST}/projects/rendition.jpg`,
      `s3://${INGEST}/projects/${PID}/image:1/rendition.jpgx`,
      `s3://${PROXY}/frames/1.jpg`,
      `s3://${INGEST}/`,
      's3://other-bucket/a.jpg',
      `s3://${INGEST}-evil/a.jpg`,
      `s3://${INGEST}.evil/a.jpg`,
      `s3://${INGEST}`,
      `S3://${INGEST}/a.jpg`,
      `https://${INGEST}.s3.amazonaws.com/a.jpg`,
      'file:///etc/passwd',
      '',
    ),
  }),
  fc.record({ value: fc.oneof(fc.integer({ min: -5, max: 5 }), fc.array(fc.string(), { maxLength: 2 }), fc.dictionary(fc.string(), fc.string(), { maxKeys: 2 })) }),
  fc.record({ value: fc.string() }),
);
const tagsArb: fc.Arbitrary<Tags> = fc.oneof(
  fc.record({ project_id: fc.constantFrom(PID, OTHERPID, PID.toUpperCase(), 'default', '*', SUB) }),
  fc.record({ project_id: fc.constantFrom(PID, OTHERPID), sub: fc.constant(SUB) }),
  fc.constant<Tags>({}), // no project_id tag at all
  fc.record({ sub: fc.constant(PID) }), // only a sub, equal to a collection id
);
const actionArb = fc.constantFrom(TOOL, ...OTHER_ACTIONS);
const resourceArb = fc.constantFrom(GW, GW + '-2', 'arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/other-gw');

fc.assert(
  fc.property(tagsArb, collectionArb, imageArb, actionArb, resourceArb, fc.option(fc.string(), { nil: undefined }), (tags, coll, img, action, resource, searchTerm) => {
    const input: Input = {};
    if (!('omit' in coll)) input['collection_id'] = coll.value;
    if (!('omit' in img)) input['search_image_s3uri'] = img.value;
    if (searchTerm !== undefined) input['search_term'] = searchTerm; // unconstrained by the policy
    const want = expectAllow(tags, input, action, resource) ? 'allow' : 'deny';
    assert.equal(decide(tags, input, action, resource), want, `oracle/engine mismatch for ${JSON.stringify({ tags, input, action, resource })}`);
  }),
  { numRuns: 500 },
);

// =============================================================================================
// Part C — the builder half of Property 8: for any candidate bucket name, the builder returns a
// statement exactly when the name is non-empty and matches ^[a-z0-9.-]+$, and otherwise throws
// naming the offending setting. (>=100 runs; the fixed throw cases live in search-policy.test.ts.)
// =============================================================================================
const BUCKET_OK = /^[a-z0-9.-]+$/;
const baseBuilder = { searchToolName: TOOL, gatewayArn: GW, envName: 'sandbox' as const };
fc.assert(
  fc.property(fc.string({ maxLength: 40 }), (name) => {
    const valid = name.length > 0 && BUCKET_OK.test(name);
    // As the Ingest bucket (proxy fixed-valid), then as the proxy bucket (ingest fixed-valid).
    if (valid) {
      const asIngest = buildSearchPolicy({ ...baseBuilder, ingestBucket: name, m2cProxyBucket: 'valid-proxy' });
      assert.ok(asIngest.includes(`s3://${name}/projects/*/rendition.jpg`), `expected ingest literal for ${JSON.stringify(name)}`);
      const asProxy = buildSearchPolicy({ ...baseBuilder, ingestBucket: 'valid-ingest', m2cProxyBucket: name });
      assert.ok(asProxy.includes(`s3://${name}/*`), `expected proxy literal for ${JSON.stringify(name)}`);
    } else {
      assert.throws(() => buildSearchPolicy({ ...baseBuilder, ingestBucket: name, m2cProxyBucket: 'valid-proxy' }), /Ingest bucket/, `expected throw naming Ingest bucket for ${JSON.stringify(name)}`);
      assert.throws(() => buildSearchPolicy({ ...baseBuilder, ingestBucket: 'valid-ingest', m2cProxyBucket: name }), /media\.proxyBucket/, `expected throw naming media.proxyBucket for ${JSON.stringify(name)}`);
    }
  }),
  { numRuns: 400 },
);

console.log('search-policy decision (Property 8): all assertions passed');
