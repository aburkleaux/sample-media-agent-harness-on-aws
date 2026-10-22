import * as path from 'path';
import { createHash } from 'crypto';
import * as cdk from 'aws-cdk-lib';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as assets from 'aws-cdk-lib/aws-s3-assets';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

const root = path.resolve(__dirname, '../../..');
const exclude = [
  '**/node_modules', '**/.venv', '**/dist', '**/server-dist', '**/.env', '**/.env.*',
  '**/__pycache__', '**/*.pyc', '**/.git', '**/test-results', '**/.pytest_cache', '**/.hypothesis',
  '**/*.egg-info',
];

/** Which artifacts to build in the deploying account. Each is skipped when CI already pushed it by tag. */
export interface HarnessAssetsProps {
  /** UI proxy image (`ui/proxy.Dockerfile`) plus the SPA (`ui/dist`), built from the same `ui/` source. */
  readonly ui?: boolean;
  /** Capability API image (`agent/capability.Dockerfile`). */
  readonly capability?: boolean;
  /** Knowledge-graph Lambda image (`agent/graph.Dockerfile --target runtime`). */
  readonly graph?: boolean;
  /**
   * The Code Interpreter sandbox's ffmpeg (`lib/distribution/ffmpeg/Dockerfile`: static LGPL FFmpeg +
   * OpenH264, arm64), built natively on ARM and staged at `s3://<bucketName>/tools/ffmpeg/` — the path
   * the Code Interpreter sandbox reads with its own IAM role.
   */
  readonly ffmpeg?: { readonly bucketName: string };
}

/** A built image: its CDK-owned repository plus the immutable tag this deployment pushed. */
export interface BuiltImage {
  readonly repository: ecr.IRepository;
  readonly tag: string;
}

/**
 * Builds the harness's container images (and the SPA) at deployment time in CodeBuild, inside the
 * deploying account — so `cdk deploy` needs no local Docker, no pre-created ECR repository and no
 * `-c ...ImageTag` context. Each build gets its own CDK-owned IMMUTABLE ECR repository (retained, so
 * prior tags stay available for rollback) and a fresh tag per deployment request; a build reruns
 * only when its source, buildspec or build image changes.
 */
export class HarnessAssets extends Construct {
  readonly uiImage?: ecs.ContainerImage;
  readonly uiSource?: s3deploy.ISource;
  readonly capabilityImage?: ecs.ContainerImage;
  readonly graphImage?: BuiltImage;
  /** Completes once the sandbox ffmpeg is staged; order it after the bucket when this stack owns it. */
  readonly ffmpegBuild?: cdk.CustomResource;

  constructor(scope: Construct, id: string, props: HarnessAssetsProps) {
    super(scope, id);
    const output = new s3.Bucket(this, 'BuildOutputs', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED, enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const code = lambda.Code.fromAsset(path.join(__dirname, 'harness-build-handler'), {
      exclude: ['__pycache__', '*.pyc'],
    });
    const onEvent = new lambda.Function(this, 'OnEvent', {
      code, runtime: lambda.Runtime.PYTHON_3_13, handler: 'index.on_event',
      timeout: cdk.Duration.seconds(60),
    });
    const isComplete = new lambda.Function(this, 'IsComplete', {
      code, runtime: lambda.Runtime.PYTHON_3_13, handler: 'index.is_complete',
      timeout: cdk.Duration.seconds(60),
    });
    const provider = new cr.Provider(this, 'Provider', {
      onEventHandler: onEvent, isCompleteHandler: isComplete,
      queryInterval: cdk.Duration.seconds(30), totalTimeout: cdk.Duration.minutes(55),
    });

    // One source archive per directory; the capability and graph images share `agent/`.
    const sources = new Map<string, assets.Asset>();
    const sourceFor = (dir: 'ui' | 'agent') => {
      let source = sources.get(dir);
      if (!source) {
        source = new assets.Asset(this, `${dir}Source`, {
          path: path.join(root, dir), exclude, ignoreMode: cdk.IgnoreMode.GLOB,
        });
        sources.set(dir, source);
      }
      return source;
    };

    const start = (name: string, project: codebuild.Project, revision: string, extra: Record<string, string>) => {
      const startGrant = iam.Grant.addToPrincipal({ grantee: onEvent, actions: ['codebuild:StartBuild'], resourceArns: [project.projectArn] });
      isComplete.addToRolePolicy(new iam.PolicyStatement({
        actions: ['codebuild:BatchGetBuilds'], resources: [project.projectArn],
      }));
      const resource = new cdk.CustomResource(this, `${name}Result`, {
        serviceToken: provider.serviceToken,
        properties: { ProjectName: project.projectName, Revision: revision, ...extra },
      });
      // Project Ref alone does not wait for updates to its buildspec or IAM policies.
      startGrant.applyBefore(resource);
      resource.node.addDependency(project);
      resource.node.addDependency(project.role!);
      resource.node.addDependency(onEvent);
      resource.node.addDependency(isComplete);
      return resource;
    };

    const build = (name: 'Ui' | 'Capability' | 'Graph', dir: 'ui' | 'agent', dockerArgs: string, extra: string[] = []) => {
      const source = sourceFor(dir);
      const repo = new ecr.Repository(this, `${name}Images`, {
        imageTagMutability: ecr.TagMutability.IMMUTABLE,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });
      const commands = [
        'set -euo pipefail',
        'aws ecr get-login-password --region "$AWS_DEFAULT_REGION" | docker login --username AWS --password-stdin "${REPOSITORY_URI%%/*}"',
        // Base images come from ECR Public's Docker Official Images mirror, avoiding Docker Hub's
        // anonymous pull limits on shared CodeBuild hosts.
        `docker build --platform linux/amd64 --provenance=false --sbom=false ${dockerArgs} -t "$REPOSITORY_URI:$IMAGE_TAG" .`,
        'docker push "$REPOSITORY_URI:$IMAGE_TAG"',
        ...extra,
      ];
      const spec = { version: '0.2', env: { shell: 'bash' }, phases: { build: { commands } } };
      const buildImage = codebuild.LinuxBuildImage.STANDARD_7_0;
      const revision = createHash('sha256').update(JSON.stringify({
        source: source.assetHash, spec, image: buildImage.imageId,
      })).digest('hex');
      const project = new codebuild.Project(this, `${name}Build`, {
        source: codebuild.Source.s3({ bucket: source.bucket, path: source.s3ObjectKey }),
        buildSpec: codebuild.BuildSpec.fromObject(spec),
        environment: { buildImage, privileged: true, computeType: codebuild.ComputeType.MEDIUM },
        timeout: cdk.Duration.minutes(40), queuedTimeout: cdk.Duration.minutes(5),
        environmentVariables: {
          REPOSITORY_URI: { value: repo.repositoryUri },
          OUTPUT_BUCKET: { value: output.bucketName },
        },
      });
      repo.grantPullPush(project);
      const resource = start(name, project, revision, {
        // Rebuild if an output destination is replaced, even with unchanged source.
        RepositoryUri: repo.repositoryUri, OutputBucket: output.bucketName,
      });
      return { repo, project, tag: resource.getAttString('ImageTag'), resource };
    };

    if (props.ui) {
      const ui = build('Ui', 'ui', '--build-arg BASE_IMAGE=public.ecr.aws/docker/library/node:22-slim -f proxy.Dockerfile', [
        // The SPA: the same Node 22 build CI runs, zipped for the BucketDeployments.
        'docker run --rm -v "$PWD:/app" -w /app -e VITE_DEV_BYPASS_AUTH=false -e DEV_BYPASS_AUTH=false public.ecr.aws/docker/library/node:22-slim bash -euc "npm ci && npm run build:deploy"',
        '(cd dist && zip -qr ../spa.zip .)',
        'aws s3 cp spa.zip "s3://$OUTPUT_BUCKET/ui/$IMAGE_TAG.zip"',
      ]);
      output.grantWrite(ui.project, 'ui/*');
      this.uiImage = ecs.ContainerImage.fromEcrRepository(ui.repo, ui.tag);
      this.uiSource = s3deploy.Source.bucket(output, ui.resource.getAttString('SpaKey'));
    }
    if (props.capability) {
      const capability = build('Capability', 'agent', '--build-arg BASE_IMAGE=public.ecr.aws/docker/library/python:3.13-slim -f capability.Dockerfile');
      this.capabilityImage = ecs.ContainerImage.fromEcrRepository(capability.repo, capability.tag);
    }
    if (props.graph) {
      const graph = build('Graph', 'agent', '--build-arg BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-trixie -f graph.Dockerfile --target runtime');
      // Lambda pulls container images with its service principal; the graph functions import this repo
      // by name in a nested stack, where CDK cannot add the policy for them.
      graph.repo.addToResourcePolicy(new iam.PolicyStatement({
        principals: [new iam.ServicePrincipal('lambda.amazonaws.com')],
        actions: ['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer'],
        conditions: {
          StringLike: { 'aws:sourceArn': cdk.Arn.format({ service: 'lambda', resource: 'function', resourceName: '*', arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME }, cdk.Stack.of(this)) },
        },
      }));
      this.graphImage = { repository: graph.repo, tag: graph.tag };
    }
    if (props.ffmpeg) {
      // Not an image: the Dockerfile's `artifact` stage exports the tarball (+ its LGPL corresponding
      // source), which is copied to the Skills bucket. A native ARM host — no QEMU for the compile.
      const source = new assets.Asset(this, 'ffmpegSource', { path: path.join(__dirname, 'ffmpeg') });
      const bucket = props.ffmpeg.bucketName;
      const spec = {
        version: '0.2', env: { shell: 'bash' }, phases: { build: { commands: [
          'set -euo pipefail',
          'DOCKER_BUILDKIT=1 docker build --platform linux/arm64 --target artifact --output type=local,dest=out .',
          'aws s3 cp out/ffmpeg-release-arm64-static.tar.xz "s3://$FFMPEG_BUCKET/tools/ffmpeg/ffmpeg-release-arm64-static.tar.xz"',
          'aws s3 cp --recursive out/sources "s3://$FFMPEG_BUCKET/tools/ffmpeg/sources/"',
        ] } },
      };
      const buildImage = codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0;
      const revision = createHash('sha256').update(JSON.stringify({
        source: source.assetHash, spec, image: buildImage.imageId,
      })).digest('hex');
      const project = new codebuild.Project(this, 'FfmpegBuild', {
        source: codebuild.Source.s3({ bucket: source.bucket, path: source.s3ObjectKey }),
        buildSpec: codebuild.BuildSpec.fromObject(spec),
        environment: { buildImage, privileged: true, computeType: codebuild.ComputeType.LARGE },
        timeout: cdk.Duration.minutes(40), queuedTimeout: cdk.Duration.minutes(5),
        environmentVariables: { FFMPEG_BUCKET: { value: bucket } },
      });
      // Identity policy: the bucket may be another stack's (MediaToolkitStack owns it for `customer`).
      project.addToRolePolicy(new iam.PolicyStatement({
        actions: ['s3:PutObject'], resources: [`arn:${cdk.Aws.PARTITION}:s3:::${bucket}/tools/ffmpeg/*`],
      }));
      this.ffmpegBuild = start('Ffmpeg', project, revision, { Bucket: bucket });
    }
  }
}
