/**
 * HarnessAssets synth assertions: images + SPA are built in CodeBuild at deploy time, never by local Docker.
 * Run: `npx ts-node test/harness-assets.test.ts` (node:assert, no framework — like the other infra tests).
 */
import * as assert from 'node:assert/strict';
import * as cdk from 'aws-cdk-lib';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as deploy from 'aws-cdk-lib/aws-s3-deployment';
import { HarnessAssets } from '../lib/distribution/harness-assets';

const app = new cdk.App();
const stack = new cdk.Stack(app, 'HarnessBuildTest');
const builds = new HarnessAssets(stack, 'Builds', { ui: true, capability: true, graph: true });
const task = new ecs.FargateTaskDefinition(stack, 'Task');
task.addContainer('Proxy', { image: builds.uiImage! });
task.addContainer('Api', { image: builds.capabilityImage! });
new deploy.BucketDeployment(stack, 'Spa', {
  destinationBucket: new s3.Bucket(stack, 'Site'), sources: [builds.uiSource!],
});
assert.ok(builds.graphImage, 'graph image exposed as repo + tag');
const template = app.synth().getStackArtifact(stack.artifactId).template;
const entries = Object.entries<any>(template.Resources);

const projects = entries.filter(([, r]) => r.Type === 'AWS::CodeBuild::Project');
assert.equal(projects.length, 3, 'ui, capability, graph');
for (const [, r] of projects) {
  const spec = r.Properties.Source.BuildSpec as string;
  assert.equal(r.Properties.Source.Type, 'S3');
  assert.equal(r.Properties.Environment.PrivilegedMode, true);
  assert.equal(r.Properties.Environment.Type, 'LINUX_CONTAINER');
  assert.equal(r.Properties.TimeoutInMinutes, 40);
  assert.match(spec, /docker build --platform linux\/amd64 --provenance=false --sbom=false/);
  assert.match(spec, /docker push/);
  assert.match(spec, /--build-arg BASE_IMAGE=public\.ecr\.aws\/docker\/library\//);
}
const specs = projects.map(([, r]) => r.Properties.Source.BuildSpec as string);
assert.equal(specs.filter((s) => s.includes('npm run build:deploy')).length, 1, 'only the UI build makes the SPA');
assert.equal(specs.filter((s) => s.includes('-f graph.Dockerfile --target runtime')).length, 1, 'graph runtime stage');

const results = entries.filter(([, r]) => r.Properties?.Revision);
assert.equal(results.length, 3);
for (const [, r] of results) {
  assert.match(r.Properties.Revision, /^[a-f0-9]{64}$/);
  assert.ok(r.DependsOn.some((id: string) => template.Resources[id].Type === 'AWS::CodeBuild::Project'));
  assert.ok(r.DependsOn.some((id: string) => template.Resources[id].Type === 'AWS::IAM::Policy'));
}
// capability + graph share one `agent/` source archive.
const sources = new Set(projects.map(([, r]) => JSON.stringify(r.Properties.Source.Location)));
assert.equal(sources.size, 2, 'ui/ and agent/ source archives');

const taskDef = entries.find(([, r]) => r.Type === 'AWS::ECS::TaskDefinition')![1];
assert.match(JSON.stringify(taskDef), /ImageTag/);
const spa = entries.find(([, r]) => r.Type === 'Custom::CDKBucketDeployment')![1];
assert.match(JSON.stringify(spa.Properties.SourceObjectKeys), /SpaKey/);
const policies = JSON.stringify(entries.filter(([, r]) => r.Type === 'AWS::IAM::Policy'));
assert.match(policies, /ecr:GetDownloadUrlForLayer/);
assert.match(policies, /codebuild:StartBuild/);
assert.match(policies, /codebuild:BatchGetBuilds/);
const repos = entries.filter(([, r]) => r.Type === 'AWS::ECR::Repository');
assert.equal(repos.length, 3);
for (const [, r] of repos) assert.equal(r.Properties.ImageTagMutability, 'IMMUTABLE');
assert.equal(repos.filter(([, r]) => JSON.stringify(r.Properties.RepositoryPolicyText ?? '').includes('lambda.amazonaws.com')).length, 1,
  'only the graph repo grants Lambda pulls');

// The sandbox ffmpeg: a native ARM, non-image build that stages the tarball + its sources on the bucket.
{
  const app3 = new cdk.App();
  const s3stack = new cdk.Stack(app3, 'Ffmpeg');
  const b3 = new HarnessAssets(s3stack, 'Builds', { ffmpeg: { bucketName: 'skills-bkt' } });
  assert.ok(b3.ffmpegBuild);
  const t3 = app3.synth().getStackArtifact(s3stack.artifactId).template;
  const all = Object.entries<any>(t3.Resources);
  const [[, p]] = all.filter(([, r]) => r.Type === 'AWS::CodeBuild::Project');
  assert.equal(p.Properties.Environment.Type, 'ARM_CONTAINER');
  assert.match(p.Properties.Environment.Image, /amazonlinux-aarch64-standard:3\.0/);
  assert.equal(p.Properties.Environment.ComputeType, 'BUILD_GENERAL1_LARGE');
  assert.equal(p.Properties.Environment.PrivilegedMode, true);
  const spec = p.Properties.Source.BuildSpec as string;
  assert.match(spec, /docker build --platform linux\/arm64 --target artifact --output type=local,dest=out \./);
  assert.match(spec, /tools\/ffmpeg\/ffmpeg-release-arm64-static\.tar\.xz/);
  assert.match(spec, /tools\/ffmpeg\/sources\//);
  assert.doesNotMatch(spec, /docker push/);
  assert.equal(all.filter(([, r]) => r.Type === 'AWS::ECR::Repository').length, 0, 'no image repo');
  const pol = JSON.stringify(all.filter(([, r]) => r.Type === 'AWS::IAM::Policy'));
  assert.match(pol, /s3:PutObject/);
  assert.match(pol, /skills-bkt\/tools\/ffmpeg\/\*/);
  const [[, res]] = all.filter(([, r]) => r.Properties?.Revision);
  assert.match(res.Properties.Revision, /^[a-f0-9]{64}$/);
  assert.equal(res.Properties.Bucket, 'skills-bkt');
}

// Only what's asked for is built.
{
  const app2 = new cdk.App();
  const s2 = new cdk.Stack(app2, 'OnlyCapability');
  const b2 = new HarnessAssets(s2, 'Builds', { capability: true });
  assert.equal(b2.uiImage, undefined); assert.equal(b2.uiSource, undefined); assert.equal(b2.graphImage, undefined); assert.equal(b2.ffmpegBuild, undefined);
  const t2 = app2.synth().getStackArtifact(s2.artifactId).template;
  assert.equal(Object.values<any>(t2.Resources).filter((r) => r.Type === 'AWS::CodeBuild::Project').length, 1);
}
console.log('harness-assets: in-stack image + SPA build synthesis passed (no local Docker).');
