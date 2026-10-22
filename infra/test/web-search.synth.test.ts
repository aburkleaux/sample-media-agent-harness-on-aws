/**
 * Synth assertions for AgentCore Web Search on the shared Platform Gateway (agent-harness-help-web-search
 * R6-R8, media-harness-web-search-toggle). Run: `npm run test:web-search-synth` (node:assert, no framework).
 *
 * Where `webSearch.enabled` (staging, prod):
 *   - exactly one `AWS::BedrockAgentCore::GatewayTarget` with `Mcp.Connector.Source.ConnectorId = web-search`,
 *     on the PLATFORM gateway (never the harness/tenant gateway), outbound auth GATEWAY_IAM_ROLE;
 *   - the Platform Gateway role may `bedrock-agentcore:InvokeWebSearch` on exactly the service-owned
 *     `arn:aws:bedrock-agentcore:<region>:aws:tool/web-search.v1`;
 *   - one Cedar permit naming EXACTLY `<project>-web-search___WebSearch` on the Platform Gateway ARN;
 *   - RuntimeConfigSecret carries WEB_SEARCH_ENABLED=true and the proxy task env WEB_SEARCH_ENABLED=true.
 * Where it is off (sandbox): none of those, and the secret says WEB_SEARCH_ENABLED=false.
 */
import * as assert from "node:assert/strict";
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AgentInfraStack } from "../lib/agent-infra-stack";
import { resolveEnvConfig, EnvName } from "../lib/env-config";

const PROJECT = "mediaworks-harness";
const TOOL = `${PROJECT}-web-search___WebSearch`;

function build(env: EnvName): Template {
  const app = new cdk.App({ context: { env, projectName: PROJECT } });
  const envConfig = resolveEnvConfig(app);
  const stack = new AgentInfraStack(app, `${PROJECT}-infra-${env}`, {
    projectName: PROJECT,
    envConfig,
    env: { account: envConfig.account, region: envConfig.region },
  });
  return Template.fromStack(stack);
}

const json = (x: unknown) => JSON.stringify(x);

function webSearchTargets(t: Template): any[] {
  return Object.values(t.findResources("AWS::BedrockAgentCore::GatewayTarget")).filter(
    (r: any) => json(r.Properties?.TargetConfiguration ?? {}).includes('"web-search"'));
}

function webSearchPermits(t: Template): any[] {
  return Object.values(t.findResources("AWS::BedrockAgentCore::Policy")).filter(
    (r: any) => json(r.Properties?.Definition ?? {}).includes("WebSearch"));
}

function invokeWebSearchStatements(t: Template): any[] {
  const out: any[] = [];
  for (const r of Object.values(t.findResources("AWS::IAM::Role")) as any[]) {
    for (const p of r.Properties?.Policies ?? []) {
      for (const s of p.PolicyDocument?.Statement ?? []) {
        const acts = Array.isArray(s.Action) ? s.Action : [s.Action];
        if (acts.includes("bedrock-agentcore:InvokeWebSearch")) out.push({ role: r, s });
      }
    }
  }
  for (const r of Object.values(t.findResources("AWS::IAM::Policy")) as any[]) {
    for (const s of r.Properties?.PolicyDocument?.Statement ?? []) {
      const acts = Array.isArray(s.Action) ? s.Action : [s.Action];
      if (acts.includes("bedrock-agentcore:InvokeWebSearch")) out.push({ role: null, s });
    }
  }
  return out;
}

function runtimeSecretString(t: Template): string {
  const secret = Object.values(t.findResources("AWS::SecretsManager::Secret")).find(
    (r: any) => json(r.Properties?.SecretString ?? "").includes("RUNTIME_ARN_PARAM_PATH")) as any;
  assert.ok(secret, "RuntimeConfigSecret exists");
  return json(secret.Properties.SecretString);
}

function proxyEnvHasFlag(t: Template): boolean {
  return Object.values(t.findResources("AWS::ECS::TaskDefinition")).some((td: any) =>
    (td.Properties?.ContainerDefinitions ?? []).some((c: any) =>
      (c.Environment ?? []).some((e: any) => e.Name === "WEB_SEARCH_ENABLED" && e.Value === "true")));
}

// ---- staging and prod: on (prod since 0.13.0) ----------------------------------------------------------
for (const env of ["staging", "prod"] as const) {
  const t = build(env);
  const resources = (t.toJSON() as any).Resources as Record<string, any>;
  const platformGwId = Object.keys(resources).find((id) =>
    resources[id].Type === "AWS::BedrockAgentCore::Gateway" && id.startsWith("PlatformGateway"));
  assert.ok(platformGwId, `${env}: the Platform Gateway exists`);

  const targets = webSearchTargets(t);
  assert.equal(targets.length, 1, `${env}: exactly one web-search connector target`);
  const tgt = targets[0];
  assert.equal(tgt.Properties.Name, `${PROJECT}-web-search`);
  assert.equal(json(tgt.Properties.GatewayIdentifier), json({ "Fn::GetAtt": [platformGwId, "GatewayIdentifier"] }),
    `${env}: the target is on the PLATFORM gateway`);
  assert.equal(tgt.Properties.TargetConfiguration.Mcp.Connector.Source.ConnectorId, "web-search");
  assert.equal(tgt.Properties.TargetConfiguration.Mcp.Connector.Configurations[0].Name, "WebSearch");
  assert.equal(json(tgt.Properties.CredentialProviderConfigurations),
    json([{ CredentialProviderType: "GATEWAY_IAM_ROLE" }]));

  const grants = invokeWebSearchStatements(t);
  assert.equal(grants.length, 1, `${env}: exactly one InvokeWebSearch grant`);
  assert.ok(grants[0].role, `${env}: the grant is inline on a role`);
  const platformRoleId = Object.keys(resources).find((id) => resources[id] === grants[0].role);
  assert.ok(platformRoleId?.startsWith("PlatformGatewayRole"), `${env}: granted to the Platform Gateway role, got ${platformRoleId}`);
  assert.ok(json(grants[0].s.Resource).includes(":aws:tool/web-search.v1"), `${env}: on the service-owned tool ARN`);
  assert.ok(!json(grants[0].s.Resource).includes("*"), `${env}: no wildcard on the web-search grant`);

  const permits = webSearchPermits(t);
  assert.equal(permits.length, 1, `${env}: exactly one web-search Cedar permit`);
  const stmt = json(permits[0].Properties.Definition);
  assert.ok(stmt.includes(`action == AgentCore::Action::\\"${TOOL}\\"`), `${env}: the permit names exactly ${TOOL}`);
  assert.ok(stmt.includes("permit("), `${env}: it is a permit`);
  assert.ok(!stmt.includes("action in"), `${env}: a single action, not a set`);

  assert.ok(runtimeSecretString(t).includes("WEB_SEARCH_ENABLED") &&
    runtimeSecretString(t).includes('\\"WEB_SEARCH_ENABLED\\":\\"true\\"'), `${env}: secret WEB_SEARCH_ENABLED=true`);
  assert.ok(proxyEnvHasFlag(t), `${env}: proxy task env WEB_SEARCH_ENABLED=true`);
}

// ---- sandbox: off -------------------------------------------------------------------------------------
for (const env of ["sandbox"] as const) {
  const t = build(env);
  assert.equal(webSearchTargets(t).length, 0, `${env}: no web-search target`);
  assert.equal(webSearchPermits(t).length, 0, `${env}: no web-search permit`);
  assert.equal(invokeWebSearchStatements(t).length, 0, `${env}: no InvokeWebSearch grant`);
  assert.ok(runtimeSecretString(t).includes('\\"WEB_SEARCH_ENABLED\\":\\"false\\"'), `${env}: secret WEB_SEARCH_ENABLED=false`);
  assert.ok(!proxyEnvHasFlag(t), `${env}: proxy env has no WEB_SEARCH_ENABLED=true`);
}

console.log("web-search synth: ok — staging and prod have target + grant + permit; sandbox has none");
