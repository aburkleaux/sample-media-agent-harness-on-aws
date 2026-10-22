/**
 * Synth assertions over the ProjectsStack template (media-harness-projects-access, design Testing
 * Strategy "Synth assertions"; task 8.12).
 *
 * Run: `npx ts-node test/projects-stack.synth.test.ts` (node:assert, no test framework — matches the
 * other infra tests). It instantiates the real `AgentInfraStack` for staging, sandbox, and prod and
 * asserts over the synthesized nested + parent templates.
 */
import * as assert from "node:assert/strict";
import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { AgentInfraStack } from "../lib/agent-infra-stack";
import { resolveEnvConfig, EnvName, EnvConfig } from "../lib/env-config";

const PROJECT = "mediaworks-harness";

interface Built {
  stack: AgentInfraStack;
  envConfig: EnvConfig;
  nested: Template | undefined;
  parent: Template;
}

function build(env: EnvName): Built {
  const app = new cdk.App({ context: { env, projectName: PROJECT } });
  const envConfig = resolveEnvConfig(app);
  const stackName = env === "dev" ? `${PROJECT}-infra` : `${PROJECT}-infra-${env}`;
  const stack = new AgentInfraStack(app, stackName, {
    projectName: PROJECT,
    envConfig,
    env: { account: envConfig.account, region: envConfig.region },
  });
  return {
    stack,
    envConfig,
    nested: stack.projectsStack ? Template.fromStack(stack.projectsStack) : undefined,
    parent: Template.fromStack(stack),
  };
}

/** Collect all resources of a type as an array of their Properties. */
function resourcesOf(t: Template, type: string): any[] {
  return Object.values(t.findResources(type)).map((r: any) => r.Properties ?? {});
}

// ------------------------------------------------------------------------------------------------
// Env matrix: nested stack in sandbox, staging, prod (since 2026-10), source.
// ------------------------------------------------------------------------------------------------
{
  const prod = build("prod");
  assert.ok(prod.stack.projectsStack, "prod must have a ProjectsStack");
  const prodJson = JSON.stringify(prod.parent.toJSON());
  assert.ok(prodJson.includes("PROJECT_TOKEN_REFRESH_FN"), "prod secret must carry PROJECT_TOKEN_REFRESH_FN");
  console.log("prod: nested stack + PROJECT_TOKEN_REFRESH_FN — ok");
}

for (const env of ["staging", "sandbox"] as const) {
  const { stack, envConfig, nested, parent } = build(env);
  assert.ok(nested, `${env} must have a ProjectsStack`);
  const t = nested!;

  // --- Projects table: PK/SK, PAY_PER_REQUEST, stream, no TTL, no GSI ---
  const tables = resourcesOf(t, "AWS::DynamoDB::Table");
  assert.equal(tables.length, 1, `${env}: exactly one projects table`);
  const table = tables[0];
  assert.equal(table.BillingMode, "PAY_PER_REQUEST", `${env}: on-demand table`);
  assert.equal(table.StreamSpecification?.StreamViewType, "NEW_AND_OLD_IMAGES", `${env}: table stream`);
  assert.ok(!("TimeToLiveSpecification" in table), `${env}: table must have no TTL (Req 1.28)`);
  assert.equal(table.PointInTimeRecoverySpecification?.PointInTimeRecoveryEnabled, true,
    `${env}: projects table has point-in-time recovery`);
  // media-harness-project-ingestion adds exactly one GSI (assetsByRegistered) — the Assets tab /
  // Dispatcher-sweep index. It must not grow a second one (CloudFormation adds one GSI per update).
  const gsis = table.GlobalSecondaryIndexes ?? [];
  assert.equal(gsis.length, 1, `${env}: exactly one GSI`);
  assert.equal(gsis[0].IndexName, "assetsByRegistered", `${env}: the assetsByRegistered GSI`);
  const gsiKeys = (gsis[0].KeySchema ?? []).reduce(
    (m: any, k: any) => ((m[k.KeyType] = k.AttributeName), m),
    {},
  );
  assert.deepEqual(gsiKeys, { HASH: "assetListPk", RANGE: "assetListSk" }, `${env}: GSI key schema`);
  const keyNames = (table.KeySchema ?? []).map((k: any) => k.AttributeName).sort();
  assert.deepEqual(keyNames, ["PK", "SK"], `${env}: PK/SK key schema`);

  // --- KMS signing key: RSA_2048 SIGN_VERIFY, TokenIssuerRole the sole kms:Sign principal ---
  const keys = resourcesOf(t, "AWS::KMS::Key");
  assert.ok(keys.length >= 1, `${env}: at least one signing key`);
  const signingKey = keys[0];
  assert.equal(signingKey.KeySpec, "RSA_2048", `${env}: RSA_2048 key spec`);
  assert.equal(signingKey.KeyUsage, "SIGN_VERIFY", `${env}: SIGN_VERIFY key usage`);
  const signStmts = (signingKey.KeyPolicy?.Statement ?? []).filter((s: any) => {
    const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
    return actions.includes("kms:Sign");
  });
  assert.ok(signStmts.length >= 1, `${env}: key policy names a kms:Sign principal`);
  // No kms:Sign statement names a principal other than an IAM role ARN (the TokenIssuerRole).
  for (const s of signStmts) {
    const principal = JSON.stringify(s.Principal ?? {});
    assert.ok(/TokenIssuerRole|:role\//.test(principal) || principal.includes("AWS"),
      `${env}: kms:Sign principal is a role (TokenIssuerRole), got ${principal}`);
  }

  // --- Both token functions on ONE shared role ---
  const fns = t.findResources("AWS::Lambda::Function");
  const issuer = Object.entries(fns).find(([id]) => id.includes("TokenIssuerFn"));
  const refresh = Object.entries(fns).find(([id]) => id.includes("TokenRefreshFn"));
  assert.ok(issuer && refresh, `${env}: both TokenIssuerFn and TokenRefreshFn exist`);
  const issuerRole = JSON.stringify((issuer![1] as any).Properties.Role);
  const refreshRole = JSON.stringify((refresh![1] as any).Properties.Role);
  assert.equal(issuerRole, refreshRole, `${env}: both token functions share one role`);
  assert.ok(/TokenIssuerRole/.test(issuerRole), `${env}: shared role is TokenIssuerRole`);

  // Handler split: issue_handler vs refresh_handler.
  assert.equal((issuer![1] as any).Properties.Handler, "index.issue_handler", `${env}: issuer handler`);
  assert.equal((refresh![1] as any).Properties.Handler, "index.refresh_handler", `${env}: refresh handler`);

  // --- Resource policies: issuer invocable by proxy role, refresh by main-agent runtime role ---
  const perms = resourcesOf(t, "AWS::Lambda::Permission");
  const permFor = (fnMatch: string) =>
    perms.filter((p) => JSON.stringify(p.FunctionName ?? "").includes(fnMatch));
  // Each token function has exactly one invoke permission here (no test invokers configured). Matched from
  // the start of the logical id (`"TokenIssuerFn…`), so `DelegatedTokenIssuerFn` is not counted as the issuer.
  assert.equal(permFor('"TokenIssuerFn').length, 1, `${env}: one invoke permission on TokenIssuerFn`);
  assert.equal(permFor('"TokenRefreshFn').length, 1, `${env}: one invoke permission on TokenRefreshFn`);

  // Delegated issuance (knowledge-graph tools for the Task Agent / plan nodes): same role, its own handler,
  // invocable by exactly one principal, verifying grants with the delegation key.
  const fnsAll = Object.entries(t.findResources("AWS::Lambda::Function")) as [string, any][];
  const delegated = fnsAll.find(([id]) => id.startsWith("DelegatedTokenIssuerFn"));
  assert.ok(delegated, `${env}: DelegatedTokenIssuerFn exists`);
  assert.equal(delegated![1].Properties.Handler, "index.delegated_issue_handler", `${env}: delegated handler`);
  assert.equal(delegated![1].Properties.FunctionName, "mediaworks-harness-project-token-delegated", `${env}: fixed name`);
  assert.ok(delegated![1].Properties.Environment.Variables.DELEGATION_KEY_ARN, `${env}: delegation key env`);
  assert.equal(permFor('"DelegatedTokenIssuerFn').length, 1, `${env}: one invoke permission on DelegatedTokenIssuerFn`);
  assert.ok(JSON.stringify(resourcesOf(t, "AWS::IAM::Policy")).includes('"kms:Verify"'), `${env}: issuer role may kms:Verify grants`);
  for (const p of [...permFor('"TokenIssuerFn'), ...permFor('"TokenRefreshFn')]) {
    assert.equal(p.Action, "lambda:InvokeFunction", `${env}: permission action`);
  }

  // --- Search_Gateway authorizer: CUSTOM_JWT, allowedAudience, discoveryUrl, NO allowedClients ---
  const gws = resourcesOf(t, "AWS::BedrockAgentCore::Gateway");
  assert.equal(gws.length, 1, `${env}: exactly one Search_Gateway`);
  const gw = gws[0];
  assert.equal(gw.AuthorizerType, "CUSTOM_JWT", `${env}: CUSTOM_JWT authorizer`);
  const jwt = gw.AuthorizerConfiguration?.CustomJWTAuthorizer ?? gw.AuthorizerConfiguration?.CustomJwtAuthorizer ?? {};
  assert.ok(Array.isArray(jwt.AllowedAudience) && jwt.AllowedAudience.length === 1,
    `${env}: authorizer has one allowedAudience`);
  assert.ok(jwt.DiscoveryUrl, `${env}: authorizer has a discoveryUrl`);
  assert.ok(!("AllowedClients" in jwt), `${env}: authorizer must NOT declare allowedClients (Req 5.1)`);
  // No searchType on the MCP protocol config (Req 6.9).
  const mcp = gw.ProtocolConfiguration?.Mcp ?? gw.ProtocolConfiguration?.mcp ?? {};
  assert.ok(!("SearchType" in mcp) && !("searchType" in mcp),
    `${env}: Search_Gateway MCP config must not declare searchType`);
  // ENFORCE override.
  assert.equal(gw.PolicyEngineConfiguration?.Mode, "ENFORCE", `${env}: policy engine ENFORCE (Req 6.1)`);

  // --- Cedar engine + policy ---
  assert.equal(resourcesOf(t, "AWS::BedrockAgentCore::PolicyEngine").length, 1, `${env}: one policy engine`);
  const policies = resourcesOf(t, "AWS::BedrockAgentCore::Policy");
  assert.equal(policies.length, 1, `${env}: exactly one Cedar policy`);
  assert.equal(policies[0].ValidationMode, "FAIL_ON_ANY_FINDINGS", `${env}: FAIL_ON_ANY_FINDINGS (Req 6.2)`);

  // Policy statement holds this env's two bucket literals.
  const expected = {
    staging: {
      ingest: "mediaworks-harness-skills-store-100000000002-us-west-2",
      proxy: "m2cv6-0e0000000002-100000000002-us-west-2-proxy",
    },
    sandbox: {
      ingest: "mediaworks-harness-skills-store-100000000001-us-west-2",
      // Sandbox's proxy bucket comes from the independently-deployed toolkit and shifts on every
      // re-harvest (`infra/media-config/sandbox.json`, gitignored, wins over the inline
      // CONFIGS.sandbox.media literal when present) — so assert against whatever this process
      // actually resolved rather than a literal that goes stale the next time someone re-harvests.
      proxy: envConfig.media?.proxyBucket,
    },
  }[env];
  assert.ok(expected.proxy, `${env}: resolved media.proxyBucket must be set`);
  const policyJson = JSON.stringify(policies[0].Definition);
  assert.ok(policyJson.includes(expected.ingest), `${env}: policy contains the Ingest bucket literal`);
  assert.ok(policyJson.includes(expected.proxy), `${env}: policy contains the proxy bucket literal`);
  assert.ok(policyJson.includes("search_content"), `${env}: policy permits the search_content action`);

  // --- Search_Gateway role actions ---
  const gwRoles = resourcesOf(t, "AWS::IAM::Role").filter((r) =>
    JSON.stringify(r.AssumeRolePolicyDocument).includes("bedrock-agentcore.amazonaws.com"),
  );
  const gwRoleJson = JSON.stringify(gwRoles.map((r) => r.Policies ?? []));
  for (const action of [
    "bedrock-agentcore:GetPolicyEngine",
    "bedrock-agentcore:AuthorizeAction",
    "bedrock-agentcore:PartiallyAuthorizeActions",
  ]) {
    assert.ok(gwRoleJson.includes(action), `${env}: Search_Gateway role has ${action} (Req 6.3)`);
  }

  // --- Parent template: secret carries PROJECT_TOKEN_REFRESH_FN; proxy role can read the secret ---
  const parentJson = JSON.stringify(parent.toJSON());
  assert.ok(parentJson.includes("PROJECT_TOKEN_REFRESH_FN"), `${env}: parent secret has PROJECT_TOKEN_REFRESH_FN`);
  assert.ok(parentJson.includes("SEARCH_GATEWAY_URL"), `${env}: parent secret has SEARCH_GATEWAY_URL`);

  console.log(`${env}: all ProjectsStack synth assertions passed`);
}

console.log("projects-stack.synth: all assertions passed");
