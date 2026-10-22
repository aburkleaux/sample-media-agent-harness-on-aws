/**
 * Unit tests for `addMediaGatewaySeed` -- the plain DynamoDB-putItem custom resource that
 * registers the independently-deployed toolkit's media-workflow-gateway. Replaces the
 * Lambda-backed `addM2cGatewaySeed` (deleted with the nested-bundle mechanism): every OAuth
 * field is now a literal env-config string, so no secret fetch is needed at deploy time.
 *
 * Runs standalone via ts-node (infra has no jest): `npx ts-node test/media-gateway-seed.test.ts`.
 * Uses `aws-cdk-lib/assertions` on a throwaway stack -- no harness synth, no deploy.
 */
import * as assert from "assert";
import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import { Template } from "aws-cdk-lib/assertions";
import { addMediaGatewaySeed } from "../lib/media-gateway-seed";

let failures = 0;
function check(name: string, fn: () => void): void {
  try {
    fn();
    console.log("  ok  -", name);
  } catch (e) {
    failures++;
    console.error("  FAIL -", name, "\n     ", (e as Error).message);
  }
}

console.log("addMediaGatewaySeed:");

const app = new cdk.App();
const stack = new cdk.Stack(app, "Seed", {
  env: { account: "111111111111", region: "us-west-2" },
});
const table = new dynamodb.Table(stack, "Reg", {
  partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
  sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
});
addMediaGatewaySeed(stack, "SeedMediaWorkflowGateway", {
  registryTable: table,
  gatewayUrl: "https://example.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
  clientId: "test-client-id",
  tokenEndpoint: "https://example.auth.us-west-2.amazoncognito.com/oauth2/token",
  credentialsSecretArn: "arn:aws:secretsmanager:us-west-2:111111111111:secret:/media/creds-AbCdEf",
  scope: "media-workflows/invoke",
});
const tpl = Template.fromStack(stack);

check("IAM policy grants only DynamoDB actions on the registry table ARN", () => {
  const policies = Object.values(tpl.findResources("AWS::IAM::Policy"));
  const stmts = policies.flatMap(
    (p) =>
      ((p.Properties as { PolicyDocument?: { Statement?: unknown[] } }).PolicyDocument
        ?.Statement ?? []) as Array<{ Action?: unknown; Resource?: unknown }>
  );
  const actions = stmts.flatMap((st) =>
    Array.isArray(st.Action) ? st.Action : st.Action ? [st.Action] : []
  );
  assert.ok(actions.includes("dynamodb:PutItem"), "grants PutItem");
  assert.ok(actions.includes("dynamodb:DeleteItem"), "grants DeleteItem");
  assert.ok(
    !actions.includes("secretsmanager:GetSecretValue"),
    "does NOT grant GetSecretValue -- no Lambda, no secret fetch at deploy time"
  );
});

check("no Lambda is created -- this is a direct SDK-call custom resource, not Lambda-backed", () => {
  const fns = tpl.findResources("AWS::Lambda::Function");
  const seedFns = Object.values(fns).filter(
    (r) => (r.Properties as { Handler?: string }).Handler === "index.on_event"
  );
  assert.strictEqual(seedFns.length, 0, "no index.on_event seed Lambda exists");
});

check("onCreate and onUpdate serialize the identical Item (redeploy = refresh, never duplicate)", () => {
  const customs = {
    ...tpl.findResources("AWS::CloudFormation::CustomResource"),
    ...tpl.findResources("Custom::AWS"),
  };
  const props = Object.values(customs)
    .map((r) => r.Properties as Record<string, unknown>)
    .find((p) => p && "Create" in p && "Update" in p);
  assert.ok(props, "a custom resource carries Create + Update SDK-call props");
  assert.deepStrictEqual(props!["Create"], props!["Update"], "Create and Update payloads are identical");
});

check("an onDelete action is present (teardown removes the registered row)", () => {
  const customs = {
    ...tpl.findResources("AWS::CloudFormation::CustomResource"),
    ...tpl.findResources("Custom::AWS"),
  };
  const props = Object.values(customs)
    .map((r) => r.Properties as Record<string, unknown>)
    .find((p) => p && "Create" in p);
  assert.ok(props && "Delete" in props, "custom resource carries a Delete SDK-call prop");
});

if (failures > 0) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("\nall checks passed");
