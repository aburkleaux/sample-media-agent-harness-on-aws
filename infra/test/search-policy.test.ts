/**
 * Unit test for the Search_Policy builder (media-harness-projects-access task 2.1).
 * Run: `npm run test:search-policy` (ts-node, node:assert — no test framework).
 *
 * The fixed sample uses the constants of `agent/scripts/local_search_gateway.py`; EXPECTED is that
 * script's `policy()` output byte for byte.
 */
import * as assert from 'node:assert/strict';
import * as cdk from 'aws-cdk-lib';
import { buildSearchPolicy } from '../lib/projects/search-policy';

const TOOL = 'mediaworks-harness-m2c-search___hybrid-search___search_content';
const GW_ARN = 'arn:aws:bedrock-agentcore:us-west-2:000000000000:gateway/local-search-gateway';
const INGEST = 'mediaworks-harness-skills-store-100000000002-us-west-2';
const PROXY = 'm2cv6-0e0000000002-100000000002-us-west-2-proxy';

const EXPECTED = `permit(
  principal,
  action == AgentCore::Action::"${TOOL}",
  resource == AgentCore::Gateway::"${GW_ARN}"
) when {
  principal.hasTag("project_id") &&
  context has input &&
  context.input has collection_id &&
  context.input.collection_id == principal.getTag("project_id") &&
  (if context.input has search_image_s3uri
     then (context.input.search_image_s3uri like "s3://${INGEST}/projects/*/rendition.jpg" ||
           context.input.search_image_s3uri like "s3://${PROXY}/*")
     else true)
};`;

const base = { searchToolName: TOOL, gatewayArn: GW_ARN, ingestBucket: INGEST, m2cProxyBucket: PROXY, envName: 'staging' };

// Fixed sample: exact statement, and never the `!(... has ...) ||` form strict validation rejects.
const out = buildSearchPolicy(base);
assert.equal(out, EXPECTED);
assert.ok(!out.includes('!('));

// Throw cases: absent, empty, and outside ^[a-z0-9.-]+$ — each naming the env and the setting.
const bad: Array<[Partial<typeof base> & { ingestBucket?: any; m2cProxyBucket?: any }, RegExp]> = [
  [{ ingestBucket: undefined }, /env "staging".*Ingest bucket/],
  [{ ingestBucket: '' }, /env "staging".*Ingest bucket/],
  [{ ingestBucket: 'Upper-Case' }, /env "staging".*Ingest bucket/],
  [{ ingestBucket: 'bucket*' }, /env "staging".*Ingest bucket/],
  [{ m2cProxyBucket: undefined }, /env "staging".*media\.proxyBucket/],
  [{ m2cProxyBucket: '' }, /env "staging".*media\.proxyBucket/],
  [{ m2cProxyBucket: 'a"b' }, /env "staging".*media\.proxyBucket/],
  [{ m2cProxyBucket: 'a\\b' }, /env "staging".*media\.proxyBucket/],
  [{ m2cProxyBucket: 'a/b' }, /env "staging".*media\.proxyBucket/],
];
for (const [override, msg] of bad) {
  assert.throws(() => buildSearchPolicy({ ...base, ...override }), msg, JSON.stringify(override));
}

// Unresolved token (the `source` env's nested M2C output): accepted, emitted via Fn::Join, and the
// resolved text equals the literal statement once the token is substituted.
const stack = new cdk.Stack(new cdk.App(), 'SearchPolicyTest');
const proxyToken = new cdk.CfnParameter(stack, 'ProxyBucket').valueAsString;
const tokenOut = buildSearchPolicy({ ...base, m2cProxyBucket: proxyToken, envName: 'source' });
assert.ok(cdk.Token.isUnresolved(tokenOut));
const resolved = stack.resolve(tokenOut);
assert.ok(resolved['Fn::Join'], JSON.stringify(resolved));
const [delim, items] = resolved['Fn::Join'];
assert.equal(delim, '');
assert.deepEqual(items.filter((i: unknown) => typeof i !== 'string'), [{ Ref: 'ProxyBucket' }]);
assert.equal(items.map((i: unknown) => (typeof i === 'string' ? i : PROXY)).join(''), EXPECTED);

console.log('search-policy: all assertions passed');
