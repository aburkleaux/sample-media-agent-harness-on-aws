/**
 * Synth assertions over the knowledge-graph GraphStack (x86 container-image writer + reader).
 * Run: `npx ts-node test/graph-stack.synth.test.ts` (node:assert, no framework — like the other synth tests).
 *
 *  - staging with graph forced ON + `graphLambdaImageTag`: GraphStack exists; both functions are x86_64
 *    container images of the same repo/tag with their own CMD; the writer has reserved concurrency 1 and a
 *    stream mapping filtered to graph-enabled META INSERT/MODIFY with a DLQ; S3 access is scoped to
 *    `projects/*\/graph/*`; the reader is invokable by the proxy role only; the proxy task carries GRAPH_FN +
 *    GRAPH_ENABLED and may invoke it. (Staging's own config parks the graph, 2026-10; forcing it on keeps the
 *    wiring tested for re-enable.)
 *  - graph ON WITHOUT the tag: GraphStack on an image HarnessAssets builds in CodeBuild (CDK-owned repo).
 *  - prod (no projects), sandbox and staging as configured (graph not enabled): no GraphStack even with a tag.
 */
import * as assert from "node:assert/strict";
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AgentInfraStack } from "../lib/agent-infra-stack";
import { resolveEnvConfig, EnvName } from "../lib/env-config";

const PROJECT = "mediaworks-harness";
const TAG = "abc1234-77";

function build(env: EnvName, tag?: string, opts: { forceGraph?: boolean } = {}) {
  const context: Record<string, unknown> = { env, projectName: PROJECT, createBuckets: true };
  if (tag) context.graphLambdaImageTag = tag;
  const app = new cdk.App({ context });
  const resolved = resolveEnvConfig(app);
  // A copy, never a mutation: the resolved config may be shared with later builds in this process.
  const envConfig = opts.forceGraph ? { ...resolved, projects: { ...resolved.projects!, graph: { enabled: true } } } : resolved;
  const stack = new AgentInfraStack(app, `${PROJECT}-infra-${env}`, {
    projectName: PROJECT, envConfig, env: { account: envConfig.account, region: envConfig.region },
  });
  return { stack, parent: Template.fromStack(stack), graph: stack.graphStack ? Template.fromStack(stack.graphStack) : undefined };
}

const props = (t: Template, type: string): any[] => Object.values(t.findResources(type)).map((r: any) => r.Properties ?? {});
const proxyEnv = (parent: Template): Record<string, string> => {
  for (const td of props(parent, "AWS::ECS::TaskDefinition")) {
    for (const c of td.ContainerDefinitions ?? []) {
      const env = Object.fromEntries((c.Environment ?? []).map((e: any) => [e.Name, e.Value]));
      if ("PROJECTS_TABLE" in env) return env;
    }
  }
  return {};
};

// ---- staging with graph forced on and the image tag -------------------------------------------
{
  const { parent, graph } = build("staging", TAG, { forceGraph: true });
  assert.ok(graph, "graph on + tag: GraphStack exists");
  const fns = props(graph!, "AWS::Lambda::Function");
  const byName = Object.fromEntries(fns.map((f) => [f.FunctionName, f]));
  const writer = byName[`${PROJECT}-graph-writer`];
  const reader = byName[`${PROJECT}-graph-reader`];
  assert.ok(writer && reader, "writer and reader have fixed names");
  for (const [f, cmd] of [[writer, "project_graph.handler.writer_handler"], [reader, "project_graph.handler.reader_handler"]] as const) {
    assert.equal(f.PackageType, "Image", "container image");
    assert.deepEqual(f.Architectures, ["x86_64"], "x86_64 (lance-graph ships no arm64 wheel)");
    assert.deepEqual(f.ImageConfig?.Command, [cmd], `CMD ${cmd}`);
    assert.ok(JSON.stringify(f.Code).includes(`${PROJECT}-graph:${TAG}`), "image is <repo>:<tag>");
  }
  assert.equal(writer.ReservedConcurrentExecutions, 1, "single writer");
  assert.equal(reader.Timeout, 60, "reader timeout = the 60 s CloudFront/ALB limit on the proxy path");
  assert.equal(reader.MemorySize, 2048, "reader memory");
  assert.equal(reader.Environment.Variables.GRAPH_WRITER_FN, `${PROJECT}-graph-writer`);

  const esm = props(graph!, "AWS::Lambda::EventSourceMapping");
  assert.equal(esm.length, 1, "one stream mapping");
  const patterns = esm[0].FilterCriteria.Filters.map((f: any) => JSON.parse(f.Pattern));
  assert.deepEqual(patterns.map((p: any) => p.eventName).sort(), [["INSERT"], ["MODIFY"]]);
  for (const p of patterns) {
    assert.deepEqual(p.dynamodb.Keys.SK.S, ["META"]);
    assert.deepEqual(p.dynamodb.NewImage.graph.BOOL, [true], "graph BOOL true filter");
  }
  assert.equal(esm[0].FunctionResponseTypes?.[0], "ReportBatchItemFailures");
  assert.ok(esm[0].DestinationConfig?.OnFailure?.Destination, "failed records go to a DLQ");

  const policies = JSON.stringify(props(graph!, "AWS::IAM::Policy"));
  assert.ok(policies.includes("/projects/*/graph/*"), "S3 object access scoped to the graph prefix");
  assert.ok(!/projects\/\*"/.test(policies), "no unscoped projects/* object grant");
  const perms = props(graph!, "AWS::Lambda::Permission");
  assert.equal(perms.length, 1, "exactly one invoke permission (the proxy role on the reader)");

  // ---- agent graph tools on the Search_Gateway (privacy exactly like search) ----
  const tools = byName[`${PROJECT}-graph-tools`];
  assert.ok(tools, "graph-tools function exists");
  assert.deepEqual(tools.Architectures, ["x86_64"]);
  assert.deepEqual(tools.ImageConfig?.Command, ["project_graph.handler.tool_handler"]);
  const targets = props(graph!, "AWS::BedrockAgentCore::GatewayTarget");
  assert.equal(targets.length, 1, "one gateway target");
  const t = targets[0];
  assert.equal(t.Name, "project-graph");
  assert.deepEqual(t.CredentialProviderConfigurations, [{ CredentialProviderType: "GATEWAY_IAM_ROLE" }]);
  const inline = t.TargetConfiguration.Mcp.Lambda.ToolSchema.InlinePayload;
  assert.deepEqual(inline.map((x: any) => x.Name).sort(),
    ["graph_context", "graph_find", "graph_neighbours", "graph_paths", "graph_query", "graph_schema"]);
  for (const x of inline) assert.ok(x.InputSchema.Required.includes("project_id"), `${x.Name}: project_id required`);
  const permits = props(graph!, "AWS::BedrockAgentCore::Policy");
  assert.equal(permits.length, 6, "one Cedar permit per graph tool");
  for (const pol of permits) {
    const stmt = JSON.stringify(pol.Definition.Cedar.Statement);
    assert.ok(stmt.includes('context.input.project_id == principal.getTag(\\"project_id\\")'), "project_id must match the token claim");
    assert.ok(/project-graph___graph_/.test(stmt), "action is project-graph___<tool>");
    assert.equal(pol.ValidationMode, "FAIL_ON_ANY_FINDINGS");
  }
  assert.ok(JSON.stringify(props(graph!, "AWS::IAM::Policy")).includes("SearchGatewayInvokeGraphTools") ||
    Object.keys(graph!.findResources("AWS::IAM::Policy")).some((k) => k.startsWith("SearchGatewayInvokeGraphTools")),
    "the Search_Gateway role may invoke the graph tools function");

  const env = proxyEnv(parent);
  assert.equal(env.GRAPH_FN, `${PROJECT}-graph-reader`);
  assert.equal(env.GRAPH_ENABLED, "true");
  assert.ok(JSON.stringify([...props(parent, "AWS::IAM::Policy"), ...props(parent, "AWS::IAM::ManagedPolicy")]).includes("InvokeGraphReader"), "proxy may invoke the reader");
}

// ---- graph on without the tag: the image is built in-stack by CodeBuild (HarnessAssets) ----------
{
  const { parent, graph } = build("staging", undefined, { forceGraph: true });
  assert.ok(graph, "no tag → GraphStack still exists, on the in-stack image");
  const projects = Object.entries<any>(parent.findResources("AWS::CodeBuild::Project"));
  const graphBuild = projects.find(([id]) => id.startsWith("HarnessBuildsGraphBuild"));
  assert.ok(graphBuild, "a Graph CodeBuild project");
  const spec = graphBuild![1].Properties.Source.BuildSpec as string;
  assert.ok(spec.includes("-f graph.Dockerfile --target runtime"), "builds graph.Dockerfile's runtime stage");
  assert.ok(spec.includes("BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-trixie"), "ECR Public base image");
  const repos = Object.entries<any>(parent.findResources("AWS::ECR::Repository"));
  const graphRepo = repos.find(([id]) => id.startsWith("HarnessBuildsGraphImages"));
  assert.ok(graphRepo, "a CDK-owned graph image repo");
  assert.ok(JSON.stringify(graphRepo![1].Properties.RepositoryPolicyText).includes("lambda.amazonaws.com"),
    "the repo lets Lambda pull (the nested stack imports it by name, so CDK cannot add this there)");
  for (const f of props(graph!, "AWS::Lambda::Function").filter((x) => x.PackageType === "Image")) {
    assert.ok(!JSON.stringify(f.Code).includes(`${PROJECT}-graph:`), "not the out-of-band CI repo");
  }
  assert.equal(proxyEnv(parent).GRAPH_FN, `${PROJECT}-graph-reader`, "proxy gets GRAPH_FN");
}

// ---- envs without the feature (staging parks it, 2026-10) ---------------------------------------
for (const env of ["prod", "sandbox", "staging"] as EnvName[]) {
  const { parent, graph } = build(env, TAG);
  assert.equal(graph, undefined, `${env}: no GraphStack`);
  if (env !== "prod") {
    const e = proxyEnv(parent);
    assert.equal(e.GRAPH_FN, undefined, `${env}: no GRAPH_FN`);
    assert.equal(e.GRAPH_ENABLED, undefined, `${env}: no GRAPH_ENABLED (so no Graph tab, no graph on new projects)`);
  }
}

console.log("graph-stack synth: ok");
