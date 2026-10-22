/**
 * The media toolkit underneath the harness (`env=customer`, `toolkit.deploy`) and the external-toolkit
 * path (`-c toolkitStackName=`). Synth only — no AWS calls.
 * Run: `npx ts-node test/toolkit-stack.test.ts` (node:assert, no framework, like the other infra tests).
 */
import * as assert from "node:assert/strict";
import * as cdk from "aws-cdk-lib";
import { buildApp } from "../lib/build-app";
import { resolveEnvConfig } from "../lib/env-config";
import { MediaToolkitStack } from "../lib/toolkit/media-toolkit-stack";
import { TOOLKIT_TEMPLATE_URLS, TOOLKIT_VERSION } from "../lib/toolkit/toolkit-release";

// `customer` takes account/region from the CLI credentials; pin them for a deterministic synth.
process.env.CDK_DEFAULT_ACCOUNT = "111111111111";
process.env.CDK_DEFAULT_REGION = "us-west-2";

const synth = (context: Record<string, unknown>) => {
  const app = new cdk.App({ context: { projectName: "mediaworks-harness", ...context } });
  const built = buildApp(app);
  const asm = app.synth();
  const tpl = (s: cdk.Stack) => asm.getStackArtifact(s.artifactId).template;
  return { built, asm, harness: tpl(built.harness), toolkit: built.toolkitStack ? tpl(built.toolkitStack) : undefined };
};
const resources = (t: any, type: string) => Object.entries<any>(t.Resources).filter(([, r]) => r.Type === type);

// ---- deployToolkit (the customer default) --------------------------------------------------------
{
  const { built, harness, toolkit } = synth({ env: "customer" });
  assert.ok(built.toolkitStack, "customer deploys the toolkit underneath");
  assert.equal(built.toolkitStack!.stackName, "mediaworks-harness-media-toolkit");
  assert.ok(built.harness.dependencies.includes(built.toolkitStack!), "harness deploys after the toolkit stack");

  // The toolkit: a native nested stack from the published per-region template, on the shared bucket.
  const [[id, nested]] = resources(toolkit, "AWS::CloudFormation::Stack");
  assert.equal(id, "Toolkit", "stable logical id — ToolkitLookup resolves the nested stack by it");
  assert.deepEqual(nested.Properties.TemplateURL, { "Fn::FindInMap": ["ToolkitTemplateUrls", { Ref: "AWS::Region" }, "url"] });
  const mapping = toolkit.Mappings.ToolkitTemplateUrls;
  assert.deepEqual(Object.keys(mapping).sort(), Object.keys(TOOLKIT_TEMPLATE_URLS).sort());
  for (const [region, url] of Object.entries(TOOLKIT_TEMPLATE_URLS)) {
    assert.equal(mapping[region].url, url);
    assert.ok(url.includes(region), `${region}: a template staged in its own region`);
  }
  const [[bucketId, bucket]] = resources(toolkit, "AWS::S3::Bucket");
  assert.deepEqual(nested.Properties.Parameters.IngestBucket, { Ref: bucketId }, "IngestBucket = the shared bucket");
  assert.equal(bucket.DeletionPolicy, "Retain");
  assert.match(JSON.stringify(bucket.Properties.BucketName), /mediaworks-harness-skills-store-111111111111-us-west-2/);
  const policy = JSON.stringify(resources(toolkit, "AWS::S3::BucketPolicy"));
  assert.match(policy, /cloudfront\.amazonaws\.com/, "the bucket owner grants the harness CDN (OAC) its reads");
  assert.ok(!Object.values<any>(toolkit.Outputs ?? {}).some((o) => o.Export), "no cross-stack Exports");

  // hybrid-search's indexer reads image renditions from the shared bucket: granted to its EXACT role,
  // resolved at deploy time from the nested toolkit (never the account root, never a bare "*").
  const rlookups = resources(toolkit, "Custom::MediaToolkitHybridSearchRole");
  assert.equal(rlookups.length, 1, "the toolkit stack resolves the hybrid-search role");
  const [rlookupId, rlookup] = rlookups[0];
  assert.deepEqual(rlookup.Properties.ToolkitStackName, { Ref: "Toolkit" }, "reads the nested toolkit, after it exists");
  assert.equal(rlookup.Properties.RoleLogicalId, "HybridSearchLambdaRole");
  assert.ok(rlookup.Properties.Revision.includes(TOOLKIT_VERSION), "re-runs when the toolkit release changes");
  const rstmts = resources(toolkit, "AWS::S3::BucketPolicy").flatMap(([, r]) => r.Properties.PolicyDocument.Statement)
    .filter((s: any) => s.Sid === "AllowM2CHybridSearchReadRenditions");
  assert.equal(rstmts.length, 1, "one rendition-read statement on the shared bucket");
  const rs = rstmts[0];
  assert.equal(rs.Effect, "Allow");
  assert.equal(rs.Action, "s3:GetObject");
  assert.deepEqual(rs.Resource, { "Fn::Join": ["", [{ "Fn::GetAtt": [bucketId, "Arn"] }, "/projects/*/rendition.jpg"]] });
  assert.deepEqual(rs.Principal, { AWS: { "Fn::GetAtt": [rlookupId, "RoleArn"] } }, "Principal = the resolved role ARN");
  assert.equal(rs.Condition, undefined, "no PrincipalArn-condition workaround");
  // Least privilege on the lookup: read-only CloudFormation on the toolkit's own stack tree + iam:GetRole on the role.
  const fnRole = Object.entries<any>(toolkit.Resources).find(([k, r]) =>
    r.Type === "AWS::IAM::Role" && k.startsWith("HybridSearchRoleOnEvent"))!;
  const rpol = resources(toolkit, "AWS::IAM::Policy").filter(([, r]) => JSON.stringify(r.Properties.Roles).includes(fnRole[0]));
  const rpst = rpol.flatMap(([, r]) => r.Properties.PolicyDocument.Statement);
  const acts = rpst.flatMap((s: any) => ([] as string[]).concat(s.Action)).sort();
  assert.deepEqual(acts, ["cloudformation:DescribeStackResource", "cloudformation:ListStackResources", "iam:GetRole"]);
  const cfnRes = JSON.stringify(rpst.find((s: any) => ([] as string[]).concat(s.Action).includes("cloudformation:ListStackResources")).Resource);
  assert.match(cfnRes, /stack\/mediaworks-harness-media-toolkit-Toolkit\/\*/);
  assert.match(cfnRes, /stack\/mediaworks-harness-media-toolkit-Toolkit-\*/);
  assert.ok(!cfnRes.includes('stack/*'), "not every stack in the account");
  assert.match(JSON.stringify(rpst.find((s: any) => s.Action === "iam:GetRole").Resource), /:role\/\*HybridSearchLambdaRole\*/);

  // The harness: wiring read at deploy time, never imported, and it does not re-create the bucket.
  const lookups = resources(harness, "Custom::MediaToolkitLookup");
  assert.equal(lookups.length, 1);
  const lp = lookups[0][1].Properties;
  assert.equal(lp.ToolkitStackName, "mediaworks-harness-media-toolkit");
  assert.equal(lp.NestedLogicalId, "Toolkit");
  assert.ok(lp.Revision.includes(TOOLKIT_VERSION), "re-runs when the toolkit release changes");
  const h = JSON.stringify(harness);
  assert.ok(!h.includes("Fn::ImportValue"), "no cross-stack imports");
  assert.ok(!resources(harness, "AWS::S3::Bucket").some(([k]) => k.startsWith("SkillsBucket")), "Skills bucket is the toolkit stack's");
  const seed = JSON.stringify(Object.entries<any>(harness.Resources).filter(([k]) => k.startsWith("SeedMediaWorkflowGateway")));
  for (const attr of ["GatewayUrl", "ClientId", "TokenEndpoint", "SecretArn"]) {
    assert.match(seed, new RegExp(`"${lookups[0][0]}","${attr}"`), `gateway seed takes ${attr} from the lookup`);
  }
  assert.ok(!h.includes("AllowMediaToolkitReaders"), "no bucket bridge: the toolkit reads the shared bucket itself");
  assert.equal(resources(harness, "Custom::MediaToolkitHybridSearchRole").length, 0, "the bucket owner (toolkit stack) grants renditions");
  assert.equal(resources(harness, "AWS::EC2::VPC").length, 1, "a fresh account gets its own VPC");
  assert.ok(Object.keys(harness.Resources).some((k) => k.startsWith("AgentRegistry")), "Agent Registry created by name");
  // The sandbox ffmpeg is built and staged on the shared bucket at deploy time.
  const ffmpeg = resources(harness, "AWS::CodeBuild::Project").filter(([k]) => k.includes("FfmpegBuild"));
  assert.equal(ffmpeg.length, 1, "customer builds the sandbox ffmpeg");
  assert.match(JSON.stringify(ffmpeg[0][1].Properties.Environment), /mediaworks-harness-skills-store-111111111111-us-west-2/);
}

// ---- no -c env= at all: the customer env, on the caller's account ------------------------------------
// A bare `cdk bootstrap` / `cdk deploy` from the shared package once resolved to the internal dev env and tried
// to bootstrap its fixed (placeholder) account with the customer's credentials.
{
  const { built, harness } = synth({});
  assert.equal(built.harness.stackName, "mediaworks-harness-infra-customer", "no env context = the customer env");
  assert.equal(built.harness.account, "111111111111", "the caller's account (CDK_DEFAULT_ACCOUNT), not an internal one");
  assert.ok(built.toolkitStack, "the toolkit is deployed underneath, as for -c env=customer");
  assert.ok(!JSON.stringify(harness).includes("100000000004"), "no internal placeholder account anywhere");
}

// ---- an existing toolkit (-c toolkitStackName=) ---------------------------------------------------
{
  const { built, harness } = synth({ env: "customer", toolkitStackName: "my-toolkit", toolkitLookupNonce: "n1" });
  assert.equal(built.toolkitStack, undefined, "no toolkit stack when one is named");
  const lp = resources(harness, "Custom::MediaToolkitLookup")[0][1].Properties;
  assert.equal(lp.ToolkitStackName, "my-toolkit");
  assert.equal(lp.NestedLogicalId, "");
  assert.equal(lp.Revision, "n1");
  assert.ok(resources(harness, "AWS::S3::Bucket").some(([k]) => k.startsWith("SkillsBucket")), "harness creates the Skills bucket");
  const [, ffmpegResult] = Object.entries<any>(harness.Resources).find(([k]) => k.includes("FfmpegResult"))!;
  assert.ok(ffmpegResult.DependsOn.some((d: string) => d.startsWith("SkillsBucket")), "ffmpeg staged after the bucket exists");
  const stmts = resources(harness, "AWS::S3::BucketPolicy").flatMap(([, r]) => r.Properties.PolicyDocument.Statement)
    .filter((s: any) => String(s.Sid).startsWith("AllowMediaToolkitReaders"));
  assert.equal(stmts.length, 2, "the toolkit's reader roles are bridged onto the Skills bucket");
  for (const s of stmts) {
    assert.ok(s.Condition.ArnEquals["aws:PrincipalArn"]["Fn::Split"], "deploy-time role list, split in CloudFormation");
    assert.equal(s.Condition.StringEquals["aws:PrincipalAccount"], "111111111111");
  }
  // The same hybrid-search role lookup, on the bucket this stack owns here.
  const [[rlId, rl]] = resources(harness, "Custom::MediaToolkitHybridSearchRole");
  assert.equal(rl.Properties.ToolkitStackName, "my-toolkit");
  assert.equal(rl.Properties.Revision, "n1");
  const rend = resources(harness, "AWS::S3::BucketPolicy").flatMap(([, r]) => r.Properties.PolicyDocument.Statement)
    .filter((s: any) => s.Sid === "AllowM2CHybridSearchReadRenditions");
  assert.equal(rend.length, 1);
  assert.deepEqual(rend[0].Principal, { AWS: { "Fn::GetAtt": [rlId, "RoleArn"] } }, "the resolved role, by ARN");
  assert.match(JSON.stringify(rend[0].Resource), /\/projects\/\*\/rendition\.jpg/);
}

// ---- outside the published regions -----------------------------------------------------------------
assert.throws(
  () => new MediaToolkitStack(new cdk.App(), "T", { projectName: "p", env: { account: "111111111111", region: "sa-east-1" } }),
  /published in .*not in sa-east-1/,
);

// ---- existing envs: untouched --------------------------------------------------------------------
for (const env of ["sandbox", "staging", "prod"]) {
  // createBuckets: as infra/cdk.json sets it for every real synth (an imported bucket carries no policy).
  const { built, harness } = synth({ env, uiProxyImageTag: "t", capabilityApiImageTag: "t", createBuckets: true });
  assert.equal(built.toolkitStack, undefined, `${env}: no toolkit stack`);
  assert.equal(resources(harness, "Custom::MediaToolkitLookup").length, 0, `${env}: static media block, no lookup`);
  assert.ok(!Object.keys(harness.Resources).some((k) => k.includes("Ffmpeg")), `${env}: no ffmpeg build`);
  assert.equal(resources(harness, "Custom::MediaToolkitHybridSearchRole").length, 0, `${env}: no role lookup`);
  // staging/prod keep their hand-listed rendition reader, exactly as before.
  const rend = resources(harness, "AWS::S3::BucketPolicy").flatMap(([, r]) => r.Properties.PolicyDocument.Statement)
    .filter((s: any) => s.Sid === "AllowM2CHybridSearchReadRenditions");
  const listed = (resolveEnvConfig(new cdk.App({ context: { env } })).media?.renditionReaderRoleArns) ?? [];
  if (listed.length === 0) {
    assert.equal(rend.length, 0, `${env}: no rendition grant`);
  } else {
    assert.equal(rend.length, 1, `${env}: one rendition grant`);
    assert.deepEqual(rend[0], {
      Action: "s3:GetObject", Effect: "Allow",
      Principal: { AWS: listed.length === 1 ? listed[0] : listed },
      Resource: rend[0].Resource, Sid: "AllowM2CHybridSearchReadRenditions",
    }, `${env}: the hand-listed role ARN(s), verbatim`);
    assert.match(JSON.stringify(rend[0].Resource), /\/projects\/\*\/rendition\.jpg"/);
  }
}
console.log("toolkit-stack: ok");
