/**
 * Synth assertions for `artifactsCdnPrivate` (env-config). Run:
 * `npx ts-node test/artifacts-cdn-private.synth.test.ts` (node:assert, no framework — matches the
 * other infra synth tests).
 *
 * The flag is ON in staging, prod and sandbox (asset-lineage Phase 3: project media is private in
 * every env that serves it), so all three must show the same locked behaviour:
 *  - the ArtifactsCdn viewer-request function denies EVERY request except the two public open-source
 *    ffmpeg release tarballs the code-interpreter sandbox downloads, and it is associated with BOTH
 *    the default behaviour and `projects/*`;
 *  - the UI proxy task carries ARTIFACTS_REQUIRE_AUTH=true;
 *  - RuntimeConfigSecret carries ARTIFACTS_CDN_PRIVATE=true, and no AgentCore runtime env does;
 *  - where the env runs Slack (staging, prod), the worker + notifier have SKILLS_BUCKET, UI_DOMAIN
 *    and s3:GetObject on the skills bucket `artifacts/*` — they read by key, never over the CDN.
 * `dev` (flag off) pins the unchanged `projects/*`-only deny: neither ARTIFACTS_REQUIRE_AUTH nor
 * ARTIFACTS_CDN_PRIVATE appears anywhere in its template.
 */
import * as assert from "node:assert/strict";
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AgentInfraStack } from "../lib/agent-infra-stack";
import { resolveEnvConfig, EnvName } from "../lib/env-config";

const PROJECT = "mediaworks-harness";

/** The pre-flag ProjectsDeny code, verbatim. Flag off must synthesize exactly this. */
const PROJECTS_ONLY_CODE = [
  "function handler(event) {",
  "  var uri = event.request.uri, path;",
  "  try { path = decodeURIComponent(uri); } catch (e) { return deny(); }",
  "  path = path.replace(/\\/{2,}/g, '/');",
  "  if (/^\\/(\\.\\/)*projects(\\/|$)/.test(path)) return deny();",
  "  return event.request;",
  "}",
  "function deny() { return { statusCode: 403, statusDescription: 'Forbidden' }; }",
].join("\n");

import * as vm from "node:vm";

/**
 * Run the synthesized CloudFront Function against a viewer-request URI. Returns "pass" when the
 * function forwards the request, or the status code it answers with.
 */
function runViewerRequest(code: string, uri: string): "pass" | number {
  const ctx: Record<string, unknown> = {};
  vm.runInNewContext(`${code}\nresult = handler({ request: { uri: ${JSON.stringify(uri)} } });`, ctx);
  const r = ctx.result as { uri?: string; statusCode?: number };
  return r && r.uri === uri ? "pass" : (r?.statusCode ?? -1);
}

/** Paths that must be refused when the CDN is private, including encoding/traversal tricks. */
const MUST_DENY = [
  "/artifacts/sess-1/output/final.mp4",
  "/artifacts/durable/abc.mp4",
  "/projects/p1/asset.mp4",
  "/%70rojects/p1/asset.mp4",
  "/tools/ffmpeg/../../artifacts/sess-1/output/final.mp4",
  "/tools/ffmpeg/..%2F..%2Fartifacts%2Fsess-1%2Foutput%2Ffinal.mp4",
  "/tools/ffmpeg/ffmpeg-release-amd64-static.tar.xz/../../../artifacts/x",
  "/tools/ffmpeg/other.tar.xz",
  "/tools/",
  "/",
  "/%E0%A4%A",
];
// asset-lineage Phase 3 (a): a session output under the project layout is NEVER served by the CDN.
MUST_DENY.push("/projects/p0123456789abcdef0123456789abcdef/sessions/4f9d0d02-1111-4222-8333-444455556666/output/clip.mp4");
// No public path at all: the sandbox ffmpeg is read by S3 key with the sandbox's IAM role.
MUST_DENY.push("/tools/ffmpeg/ffmpeg-release-amd64-static.tar.xz", "/tools/ffmpeg/ffmpeg-release-arm64-static.tar.xz");

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

/** [logicalId, properties] of the ArtifactsCdn deny function. */
function denyFunction(t: Template, env: string): [string, any] {
  const fns = Object.entries(t.findResources("AWS::CloudFront::Function")).filter(([id]) =>
    id.startsWith("ProjectsDeny"),
  );
  assert.equal(fns.length, 1, `${env}: exactly one ProjectsDeny CloudFront Function`);
  return [fns[0][0], (fns[0][1] as any).Properties];
}

/** The ArtifactsCdn distribution config (the one with the `projects/*` behaviour). */
function artifactsCdn(t: Template, env: string): any {
  const dist = Object.values(t.findResources("AWS::CloudFront::Distribution"))
    .map((r: any) => r.Properties.DistributionConfig)
    .find((c: any) => (c.CacheBehaviors ?? []).some((b: any) => b.PathPattern === "projects/*"));
  assert.ok(dist, `${env}: ArtifactsCdn distribution exists`);
  return dist;
}

function assertAssociated(cfg: any, fnLogicalId: string, env: string): void {
  const behaviours = [
    ["default", cfg.DefaultCacheBehavior],
    ["projects/*", cfg.CacheBehaviors.find((b: any) => b.PathPattern === "projects/*")],
  ];
  for (const [name, b] of behaviours) {
    const assoc = (b.FunctionAssociations ?? []).filter((a: any) => a.EventType === "viewer-request");
    assert.equal(assoc.length, 1, `${env}: ${name} behaviour has one viewer-request function`);
    assert.ok(
      JSON.stringify(assoc[0].FunctionARN).includes(fnLogicalId),
      `${env}: ${name} behaviour uses ${fnLogicalId}`,
    );
  }
}

/** Environment of the UI proxy container (the one with USER_POOL_ISSUER + SESSIONS_TABLE). */
function proxyEnv(t: Template, env: string): Record<string, unknown> {
  for (const r of Object.values(t.findResources("AWS::ECS::TaskDefinition")) as any[]) {
    for (const c of r.Properties.ContainerDefinitions ?? []) {
      const vars = Object.fromEntries((c.Environment ?? []).map((e: any) => [e.Name, e.Value]));
      // AGENT_RUNTIME_ARN is no longer task env (it comes from SSM at startup); SESSIONS_TABLE is proxy-only.
      if ("USER_POOL_ISSUER" in vars && "SESSIONS_TABLE" in vars) return vars;
    }
  }
  assert.fail(`${env}: UI proxy task definition not found`);
}

function lambdaByName(t: Template, suffix: string): [string, any] | undefined {
  const hit = Object.entries(t.findResources("AWS::Lambda::Function")).find(
    ([, r]: [string, any]) => r.Properties?.FunctionName === `${PROJECT}-${suffix}`,
  );
  return hit ? [hit[0], (hit[1] as any).Properties] : undefined;
}

// ------------------------------------------------------------------------------------------------
// staging, prod, sandbox: everything locked.
// ------------------------------------------------------------------------------------------------
for (const env of ["staging", "prod", "sandbox"] as const) {
  const t = build(env);
  const [fnId, fn] = denyFunction(t, env);
  const code = String(fn.FunctionCode);
  for (const uri of MUST_DENY) {
    assert.equal(runViewerRequest(code, uri), 403, `${env}: ${uri} must be refused (403)`);
  }
  // ...and the sandbox reads the staged ffmpeg with its own role instead.
  const policies = JSON.stringify(t.findResources("AWS::IAM::Policy"));
  assert.ok(policies.includes("/tools/ffmpeg/*"), `${env}: code interpreter role reads tools/ffmpeg/*`);
  assertAssociated(artifactsCdn(t, env), fnId, env);

  assert.equal(proxyEnv(t, env).ARTIFACTS_REQUIRE_AUTH, "true", `${env}: proxy has ARTIFACTS_REQUIRE_AUTH=true`);

  const secrets = Object.entries(t.findResources("AWS::SecretsManager::Secret")).filter(([id]) =>
    id.startsWith("RuntimeConfigSecret"),
  );
  assert.equal(secrets.length, 1, `${env}: one RuntimeConfigSecret`);
  assert.ok(
    JSON.stringify(secrets[0][1]).includes('\\"ARTIFACTS_CDN_PRIVATE\\":\\"true\\"'),
    `${env}: RuntimeConfigSecret carries ARTIFACTS_CDN_PRIVATE=true`,
  );
  for (const [id, r] of Object.entries(t.findResources("AWS::BedrockAgentCore::Runtime"))) {
    assert.ok(
      !JSON.stringify((r as any).Properties?.EnvironmentVariables ?? {}).includes("ARTIFACTS_CDN_PRIVATE"),
      `${env}: ${id} must not carry ARTIFACTS_CDN_PRIVATE in runtime env (V2 1536-byte cap)`,
    );
  }

  // Slack Lambdas read artifacts by key from the skills bucket. Only envs with a `slack` block have
  // them (staging + prod today); sandbox has none, and that is not a gap.
  const hasSlack = lambdaByName(t, "slack-worker") !== undefined;
  if (hasSlack) {
    for (const suffix of ["slack-worker", "slack-notifier"]) {
      const hit = lambdaByName(t, suffix);
      assert.ok(hit, `${env}: ${suffix} exists`);
      const [, props] = hit!;
      assert.ok(props.Environment?.Variables?.SKILLS_BUCKET, `${env}: ${suffix} has SKILLS_BUCKET`);
      // Posted replies link the UI opener (`https://<UI_DOMAIN>/open?key=`), not the private CDN.
      assert.equal(props.Environment?.Variables?.ARTIFACTS_CDN_PRIVATE, "true", `${env}: ${suffix} ARTIFACTS_CDN_PRIVATE=true`);
      assert.ok(
        JSON.stringify(props.Environment?.Variables?.UI_DOMAIN ?? "").includes("UiDistribution"),
        `${env}: ${suffix} UI_DOMAIN is the UI distribution's domain`,
      );
      const roleId = JSON.stringify(props.Role).match(/"(SlackIntegration[A-Za-z0-9]*ServiceRole[A-Za-z0-9]*)"/)?.[1];
      assert.ok(roleId, `${env}: ${suffix} role logical id`);
      const policy = Object.values(t.findResources("AWS::IAM::Policy")).find((p: any) =>
        JSON.stringify(p.Properties.Roles).includes(roleId!),
      ) as any;
      assert.ok(policy, `${env}: ${suffix} has a role policy`);
      const read = policy.Properties.PolicyDocument.Statement.find((s: any) => s.Sid === "SlackArtifactRead");
      assert.ok(read, `${env}: ${suffix} has SlackArtifactRead`);
      assert.equal(read.Action, "s3:GetObject", `${env}: ${suffix} read is s3:GetObject only`);
      assert.ok(JSON.stringify(read.Resource).includes("/artifacts/*"), `${env}: ${suffix} read scoped to artifacts/*`);
    }
    assert.ok(JSON.stringify(t.findResources("AWS::IAM::Policy")).includes("SlackArtifactRead"),
      `${env}: Slack artifact read grant present`);
  }
  console.log(`${env}: artifacts CDN denies all; proxy gated; runtime config flagged${hasSlack ? "; Slack reads by key" : ""} — ok`);
}

// ------------------------------------------------------------------------------------------------
// dev (flag absent): the unchanged `projects/*`-only deny, and no trace of the flag.
// ------------------------------------------------------------------------------------------------
{
  const env = "dev";
  const t = build(env);
  const [fnId, fn] = denyFunction(t, env);
  assert.equal(fn.FunctionCode, PROJECTS_ONLY_CODE, `${env}: ProjectsDeny code unchanged (projects/* only)`);
  assert.equal(
    fn.FunctionConfig?.Comment,
    `${PROJECT} refuse /projects/* on the artifacts CDN (project media is never public)`,
    `${env}: ProjectsDeny comment unchanged`,
  );
  assertAssociated(artifactsCdn(t, env), fnId, env);
  assert.equal(proxyEnv(t, env).ARTIFACTS_REQUIRE_AUTH, undefined, `${env}: proxy has no ARTIFACTS_REQUIRE_AUTH`);
  const json = JSON.stringify(t.toJSON());
  assert.ok(!json.includes("ARTIFACTS_REQUIRE_AUTH"), `${env}: no ARTIFACTS_REQUIRE_AUTH anywhere`);
  assert.ok(!json.includes("ARTIFACTS_CDN_PRIVATE"), `${env}: no ARTIFACTS_CDN_PRIVATE anywhere`);
  console.log(`${env}: artifacts CDN public (projects/* deny only), proxy ungated — ok`);
}

// ------------------------------------------------------------------------------------------------
// asset-lineage Phase 3 (a): sessionOutputLayout (staging only) rides RuntimeConfigSecret + the two
// importer Lambdas, never runtime env; and synth refuses it without the private CDN.
// ------------------------------------------------------------------------------------------------
for (const env of ["staging", "prod", "sandbox"] as const) {
  const t = build(env);
  const secret = JSON.stringify(Object.entries(t.findResources("AWS::SecretsManager::Secret"))
    .find(([id]) => id.startsWith("RuntimeConfigSecret"))![1]);
  const inSecret = secret.includes('\\"SESSION_OUTPUT_LAYOUT\\":\\"project\\"');
  assert.equal(inSecret, env === "staging", `${env}: SESSION_OUTPUT_LAYOUT=project in RuntimeConfigSecret only in staging`);
  for (const [id, r] of Object.entries(t.findResources("AWS::BedrockAgentCore::Runtime"))) {
    assert.ok(!JSON.stringify((r as any).Properties?.EnvironmentVariables ?? {}).includes("SESSION_OUTPUT_LAYOUT"),
      `${env}: ${id} must not carry SESSION_OUTPUT_LAYOUT in runtime env (V2 1536-byte cap)`);
  }
  for (const suffix of ["plan-chainer", "job-polling-worker"]) {
    const hit = lambdaByName(t, suffix);
    assert.ok(hit, `${env}: ${suffix} exists`);
    assert.equal(hit![1].Environment?.Variables?.SESSION_OUTPUT_LAYOUT, env === "staging" ? "project" : undefined,
      `${env}: ${suffix} SESSION_OUTPUT_LAYOUT`);
  }
  console.log(`${env}: session output layout ${env === "staging" ? "project" : "legacy"} — ok`);
}
{
  const app = new cdk.App({ context: { env: "dev", projectName: PROJECT } });
  const envConfig = { ...resolveEnvConfig(app), sessionOutputLayout: "project" as const, artifactsCdnPrivate: false };
  assert.throws(
    () => new AgentInfraStack(app, `${PROJECT}-infra-dev`, {
      projectName: PROJECT, envConfig, env: { account: envConfig.account, region: envConfig.region } }),
    /sessionOutputLayout=project requires artifactsCdnPrivate/,
    "synth refuses the project layout without the private CDN",
  );
  console.log("dev + sessionOutputLayout=project without artifactsCdnPrivate: synth refuses — ok");
}

console.log("artifacts-cdn-private.synth: all assertions passed");
