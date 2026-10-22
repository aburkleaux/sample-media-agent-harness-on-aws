// Emits the knowledge-graph tool permits (lib/graph/graph-policy.ts) so CI validates them in Cedar STRICT mode
// (test_graph_policy_strict.py) — the engine uses FAIL_ON_ANY_FINDINGS, so a flagged statement would otherwise
// fail at deploy. Writes infra/test/fixtures/graph-policy.generated.json:
//   gatewayArn, tools[]  {name, action, inputSchema, policy}   (inputSchema from agent/src/project_graph/tool_schema.json)
// Run: `npm run test:emit-graph-policy`.
import * as esbuild from 'esbuild';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import { readFileSync, writeFileSync, rmSync, mkdirSync } from 'node:fs';

const here = dirname(fileURLToPath(import.meta.url));
const res = await esbuild.build({
  entryPoints: [join(here, '..', 'lib', 'graph', 'graph-policy.ts')], bundle: true, format: 'esm', platform: 'node',
  write: false, external: ['aws-cdk-lib'], logLevel: 'silent',
});
const tmp = join(here, `.graph-policy.import.${process.pid}.mjs`);
writeFileSync(tmp, res.outputFiles[0].text);
try {
  const { buildGraphToolPolicy, graphActionName } = await import(pathToFileURL(tmp).href);
  const GATEWAY_ARN = 'arn:aws:bedrock-agentcore:us-west-2:000000000000:gateway/mediaworks-harness-search-gateway';
  const specs = JSON.parse(readFileSync(join(here, '..', '..', 'agent', 'src', 'project_graph', 'tool_schema.json'), 'utf8'));
  const tools = specs.map((s) => ({
    name: s.name, action: graphActionName(s.name), inputSchema: s.inputSchema, policy: buildGraphToolPolicy(s.name, GATEWAY_ARN),
  }));
  mkdirSync(join(here, 'fixtures'), { recursive: true });
  const out = join(here, 'fixtures', 'graph-policy.generated.json');
  writeFileSync(out, JSON.stringify({ gatewayArn: GATEWAY_ARN, tools }, null, 2) + '\n');
  console.log(`emit-graph-policy: wrote ${tools.length} statements to ${out}`);
} finally {
  rmSync(tmp, { force: true });
}
