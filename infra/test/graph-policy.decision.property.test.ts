/**
 * Cedar DECISION property test for the knowledge-graph tool permits (`lib/graph/graph-policy.ts`), evaluated
 * with the real engine (`@cedar-policy/cedar-wasm`) exactly like `search-policy.decision.property.test.ts`.
 *
 * With ALL graph permits loaded together (as on the deployed engine), a call is allowed iff it is one of the
 * graph tools on THIS gateway, the principal carries a string `project_id` tag, and `context.input.project_id`
 * is a string equal to it. Everything else — another Project, a missing/empty/non-string id, case variants,
 * prefixes, a principal with no tag, another gateway, another (search or unknown) action — is denied.
 *
 * Run: `npm run test:graph-policy-decision`.
 */
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as fc from 'fast-check';
import { buildGraphToolPolicy, graphActionName } from '../lib/graph/graph-policy';

interface CedarAnswer { readonly type: 'success' | 'failure'; readonly response?: { readonly decision: 'allow' | 'deny' }; readonly errors?: unknown[] }
// eslint-disable-next-line @typescript-eslint/no-var-requires
const cedar: { isAuthorized(call: unknown): CedarAnswer } = require('@cedar-policy/cedar-wasm/nodejs');

const SPECS = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'agent', 'src', 'project_graph', 'tool_schema.json'), 'utf8'));
const TOOLS: string[] = SPECS.map((s: { name: string }) => s.name);
const ACTIONS = TOOLS.map(graphActionName);
const GW = 'arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/mediaworks-harness-search-gateway';
const OTHER_GW = 'arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/someone-elses-gateway';
const POLICIES = TOOLS.map((t) => buildGraphToolPolicy(t, GW)).join('\n');

const PID = 'p' + 'a'.repeat(32);
const OTHER = 'p' + 'b'.repeat(32);

function decide(tags: Record<string, unknown>, input: Record<string, unknown>, action: string, resource = GW): 'allow' | 'deny' {
  const ans = cedar.isAuthorized({
    principal: { type: 'AgentCore::OAuthUser', id: 'u1' },
    action: { type: 'AgentCore::Action', id: action },
    resource: { type: 'AgentCore::Gateway', id: resource },
    context: { input },
    policies: { staticPolicies: POLICIES },
    entities: [
      { uid: { type: 'AgentCore::OAuthUser', id: 'u1' }, attrs: {}, parents: [], tags },
      { uid: { type: 'AgentCore::Gateway', id: resource }, attrs: {}, parents: [] },
    ],
  });
  assert.equal(ans.type, 'success', `request parse failure: ${JSON.stringify(ans.errors)}`);
  return ans.response!.decision;
}

function oracle(tags: Record<string, unknown>, input: Record<string, unknown>, action: string, resource: string): boolean {
  if (!ACTIONS.includes(action) || resource !== GW) return false;
  const pid = tags['project_id'];
  return typeof pid === 'string' && typeof input['project_id'] === 'string' && input['project_id'] === pid;
}

// ---- builder shape -------------------------------------------------------------------------------
assert.equal(TOOLS.length, 6, 'six graph tools');
for (const p of POLICIES.split('\n};').filter((x) => x.trim())) {
  assert.ok(p.includes('context.input.project_id == principal.getTag("project_id")'), 'project_id must match the claim');
}
assert.throws(() => graphActionName('Drop Tables'), /snake_case/);

// ---- enumerated cases ----------------------------------------------------------------------------
for (const action of ACTIONS) {
  assert.equal(decide({ project_id: PID }, { project_id: PID, text: 'x' }, action), 'allow', `${action}: own project`);
  assert.equal(decide({ project_id: PID }, { project_id: OTHER }, action), 'deny', `${action}: other project`);
  assert.equal(decide({ project_id: PID }, {}, action), 'deny', `${action}: omitted project_id`);
  assert.equal(decide({ project_id: PID }, { project_id: '' }, action), 'deny', `${action}: empty`);
  assert.equal(decide({ project_id: PID }, { project_id: 7 }, action), 'deny', `${action}: non-string`);
  assert.equal(decide({ project_id: PID }, { project_id: PID.toUpperCase() }, action), 'deny', `${action}: case variant`);
  assert.equal(decide({ project_id: PID }, { project_id: PID.slice(0, -1) }, action), 'deny', `${action}: prefix`);
  assert.equal(decide({ project_id: PID }, { project_id: PID + 'x' }, action), 'deny', `${action}: suffix`);
  assert.equal(decide({}, { project_id: PID }, action), 'deny', `${action}: principal without a project_id tag`);
  assert.equal(decide({ sub: PID }, { project_id: PID }, action), 'deny', `${action}: sub is not project_id`);
  assert.equal(decide({ project_id: PID }, { project_id: PID }, action, OTHER_GW), 'deny', `${action}: other gateway`);
}
assert.equal(decide({ project_id: PID }, { project_id: PID }, 'project-graph___graph_write'), 'deny', 'unknown graph action');
assert.equal(decide({ project_id: PID }, { project_id: PID, collection_id: PID },
  'mediaworks-harness-m2c-search___hybrid-search___search_content'), 'deny', 'graph permits do not permit search');

// ---- generated cases -----------------------------------------------------------------------------
const idArb = fc.oneof(fc.constant(PID), fc.constant(OTHER), fc.constant(PID.toUpperCase()), fc.string(), fc.integer());
// A principal tag is a JWT claim the token issuer mints: always a string. Generating an integer tag too let a
// seed pair it with the same integer input, which Cedar rightly allows (Long == Long) and the string-only
// oracle called a denial (CI seed 517530102). Inputs keep integers, so string-vs-number is still denied.
const tagArb = fc.oneof(fc.constant(PID), fc.constant(OTHER), fc.constant(PID.toUpperCase()), fc.string());
fc.assert(
  fc.property(
    fc.record({ project_id: tagArb }, { requiredKeys: [] }),
    fc.record({ project_id: idArb, text: fc.string() }, { requiredKeys: [] }),
    fc.constantFrom(...ACTIONS, 'project-graph___nope', 'other___graph_find'),
    fc.constantFrom(GW, OTHER_GW),
    (tags, input, action, resource) => {
      const got = decide(tags, input, action, resource) === 'allow';
      assert.equal(got, oracle(tags, input, action, resource), JSON.stringify({ tags, input, action, resource }));
    },
  ),
  { numRuns: 400 },
);

console.log(`graph-policy decision: ok (${ACTIONS.length} permits, enumerated + 400 generated cases)`);
