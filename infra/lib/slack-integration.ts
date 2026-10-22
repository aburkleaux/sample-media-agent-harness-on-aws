import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { NodejsFunction } from "aws-cdk-lib/aws-lambda-nodejs";
import { HttpApi, HttpMethod } from "aws-cdk-lib/aws-apigatewayv2";
import { HttpLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { HttpLambdaAuthorizer, HttpLambdaResponseType } from "aws-cdk-lib/aws-apigatewayv2-authorizers";
import * as iam from "aws-cdk-lib/aws-iam";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";
import * as path from "path";

/**
 * Additive, demonstration-only Slack ingress for the Media Agent Harness.
 *
 * Additive: HTTP API ingress + worker + notifier Lambdas, a Secrets Manager
 * entry, and a small thread-map table. It references (never modifies) the
 * existing Main Agent runtime + job-registry stream. Only instantiated when
 * `envConfig.slack.enabled` (staging).
 *
 * Ingress = API Gateway HTTP API `POST /slack/events`, AuthorizationType NONE,
 * HMAC verified in the Lambda (Slack's signing-secret scheme: the v0 signature over the raw body).
 *
 * Feature 1 (async push): the `slack-notifier` is a SECOND consumer of the
 * existing job-registry DynamoDB stream; on a terminal job for a Slack session
 * it posts the result back to the originating thread (looked up in the
 * thread-map the worker writes each turn).
 *
 * Feature 3 (native upload): worker + notifier upload result artifacts <=
 * `maxUploadBytes` into the thread; larger ones stay as CloudFront links.
 */
export interface SlackIntegrationProps {
  projectName: string;
  envName: string;
  /** Existing Main Agent runtime ARN — the only invocation target. */
  mainAgentRuntimeArn: string;
  /** Shared demo actor stamped as __actor_id on every Slack turn. */
  demoActorId: string;
  /** Session salt (namespaces/threads reset if changed). */
  sessionSalt: string;
  /** Base URL of the harness UI for /render-view links; empty = omit links. */
  rendererBaseUrl: string;
  /** CloudFront artifacts domain — used to detect artifact URLs to upload. */
  artifactsDomain: string;
  /**
   * The UI CloudFront domain (no scheme). With `artifactsCdnPrivate`, posted reply text links our artifacts
   * as `https://<uiDomain>/open?key=<key>` instead of the (403ing) CDN URL.
   */
  uiDomain: string;
  /** The env's `artifactsCdnPrivate` flag (`ARTIFACTS_CDN_PRIVATE` on the Lambdas). */
  artifactsCdnPrivate: boolean;
  /** Max artifact size (bytes) to upload natively into Slack; larger => link. */
  maxUploadBytes: number;
  /** EXISTING job-registry table (its stream drives async completion pushes). */
  jobRegistryTable: dynamodb.ITable;
  /** Artifacts bucket (skills store) — Slack file ingest writes under artifacts input prefixes. */
  artifactsBucket: s3.IBucket;
  /** File-ingest caps. */
  maxFiles: number;
  maxFileBytes: number;
  maxAggregateBytes: number;
}

export class SlackIntegration extends Construct {
  readonly secret: secretsmanager.Secret;
  readonly eventsUrl: string;

  constructor(scope: Construct, id: string, props: SlackIntegrationProps) {
    super(scope, id);

    // Placeholder secret — real values written out-of-band post-deploy.
    this.secret = new secretsmanager.Secret(this, "SlackSecret", {
      secretName: `${props.projectName}-slack-${props.envName}`,
      description:
        "Slack signing secret + bot token as JSON {signingSecret, botToken, botUserId}. " +
        "Populate via: aws secretsmanager put-secret-value.",
      secretStringValue: cdk.SecretValue.unsafePlainText(
        JSON.stringify({ signingSecret: "REPLACE_ME", botToken: "REPLACE_ME", botUserId: "" }),
      ),
    });

    // Thread map: sessionId -> {team, channel, thread} + notifier dedupe rows.
    const threadMap = new dynamodb.Table(this, "SlackThreadMap", {
      partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "expiresAt",
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    const commonBundling = { externalModules: [] as string[] };
    const commonEnv: Record<string, string> = {
      SLACK_SECRET_ARN: this.secret.secretArn,
      MAIN_AGENT_RUNTIME_ARN: props.mainAgentRuntimeArn,
      SLACK_DEMO_ACTOR_ID: props.demoActorId,
      SLACK_SESSION_SALT: props.sessionSalt,
      RENDERER_BASE_URL: props.rendererBaseUrl,
      ARTIFACTS_DOMAIN: props.artifactsDomain,
      // artifactsCdnPrivate only: posted replies link the UI opener (`/open?key=`) instead of the CDN.
      // Absent otherwise, so a flag-off env's template is unchanged.
      ...(props.artifactsCdnPrivate ? { ARTIFACTS_CDN_PRIVATE: "true", UI_DOMAIN: props.uiDomain } : {}),
      // Reply artifacts are read by KEY from this bucket (HeadObject/GetObject), never over the
      // artifacts CDN, which may be private (`artifactsCdnPrivate`).
      SKILLS_BUCKET: props.artifactsBucket.bucketName,
      MAX_UPLOAD_BYTES: String(props.maxUploadBytes),
      THREAD_MAP_TABLE: threadMap.tableName,
    };
    const readArtifacts = new iam.PolicyStatement({
      sid: "SlackArtifactRead",
      actions: ["s3:GetObject"],
      resources: [
        `${props.artifactsBucket.bucketArn}/artifacts/*`,
        // asset-lineage Phase 3 (a): session outputs under the project layout (output sub-tree only).
        `${props.artifactsBucket.bucketArn}/projects/*/sessions/*/output/*`,
      ],
    });
    const invokeMainAgent = new iam.PolicyStatement({
      sid: "InvokeMainAgentOnly",
      actions: ["bedrock-agentcore:InvokeAgentRuntime"],
      resources: [props.mainAgentRuntimeArn, `${props.mainAgentRuntimeArn}/*`],
    });

    // Worker — invokes the Main Agent, posts reply, uploads artifacts, and
    // records the session->thread map for later push.
    const worker = new NodejsFunction(this, "SlackWorker", {
      functionName: `${props.projectName}-slack-worker`,
      runtime: lambda.Runtime.NODEJS_22_X,
      entry: path.join(__dirname, "lambdas/slack-worker/handler.ts"),
      handler: "handler",
      timeout: cdk.Duration.seconds(900),
      memorySize: 1024,
      bundling: commonBundling,
      environment: {
        ...commonEnv,
        MAX_FILES: String(props.maxFiles),
        MAX_FILE_BYTES: String(props.maxFileBytes),
        MAX_AGGREGATE_BYTES: String(props.maxAggregateBytes),
      },
      description: "Demo-only Slack adapter: ingest files + invoke Main Agent + post/upload reply.",
    });
    this.secret.grantRead(worker);
    worker.addToRolePolicy(invokeMainAgent);
    worker.addToRolePolicy(readArtifacts);
    threadMap.grantWriteData(worker);
    // File ingest: write ONLY canonical input objects (streamed multipart).
    worker.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "SlackInputUpload",
        actions: ["s3:PutObject", "s3:AbortMultipartUpload"],
        resources: [`${props.artifactsBucket.bucketArn}/artifacts/*/input/*`],
      }),
    );

    // Ingress — verify + normalize + async-invoke worker + ACK.
    const ingress = new NodejsFunction(this, "SlackIngress", {
      functionName: `${props.projectName}-slack-ingress`,
      runtime: lambda.Runtime.NODEJS_22_X,
      entry: path.join(__dirname, "lambdas/slack-ingress/handler.ts"),
      handler: "handler",
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      bundling: commonBundling,
      environment: {
        SLACK_SECRET_ARN: this.secret.secretArn,
        WORKER_FUNCTION_NAME: worker.functionName,
      },
      description: "Demo-only Slack adapter: signed ingress + fast ACK.",
    });
    this.secret.grantRead(ingress);
    worker.grantInvoke(ingress);

    // Request authorizer — edge layer for the Slack webhook. HTTP API v2 authorizers do NOT
    // receive the body, so the full HMAC stays in the ingress handler; the authorizer enforces
    // what it can at the edge: signature + timestamp headers present (via identitySource), a fresh
    // (non-replayed) timestamp, and a well-formed signature. Flips the route to CUSTOM auth.
    const authorizerFn = new NodejsFunction(this, "SlackAuthorizer", {
      functionName: `${props.projectName}-slack-authorizer`,
      runtime: lambda.Runtime.NODEJS_22_X,
      entry: path.join(__dirname, "lambdas/slack-authorizer/handler.ts"),
      handler: "handler",
      timeout: cdk.Duration.seconds(5),
      memorySize: 128,
      bundling: commonBundling,
      description: "Demo-only Slack adapter: edge request authorizer (header presence, timestamp freshness, signature format).",
    });
    const slackAuthorizer = new HttpLambdaAuthorizer("SlackRequestAuthorizer", authorizerFn, {
      authorizerName: `${props.projectName}-slack-authorizer`,
      responseTypes: [HttpLambdaResponseType.SIMPLE],
      // A request missing either header is 401'd by API Gateway before the authorizer runs.
      identitySource: ["$request.header.X-Slack-Signature", "$request.header.X-Slack-Request-Timestamp"],
      // Each Slack signature is unique per request — never cache an authorization decision.
      resultsCacheTtl: cdk.Duration.seconds(0),
    });

    const httpApi = new HttpApi(this, "SlackHttpApi", {
      apiName: `${props.projectName}-slack-api`,
      description:
        "Slack Events ingress (demo-only). Route authorized by a Lambda request authorizer " +
        "(header presence + timestamp freshness + signature format); full HMAC verified in the Lambda.",
    });
    httpApi.addRoutes({
      path: "/slack/events",
      methods: [HttpMethod.POST],
      integration: new HttpLambdaIntegration("SlackIngressIntegration", ingress),
      authorizer: slackAuthorizer,
    });
    this.eventsUrl = `${httpApi.apiEndpoint}/slack/events`;

    // Notifier — SECOND consumer of the EXISTING job-registry stream. Posts
    // terminal-status results of Slack-origin jobs back to the mapped thread.
    const notifier = new NodejsFunction(this, "SlackNotifier", {
      functionName: `${props.projectName}-slack-notifier`,
      runtime: lambda.Runtime.NODEJS_22_X,
      entry: path.join(__dirname, "lambdas/slack-notifier/handler.ts"),
      handler: "handler",
      timeout: cdk.Duration.seconds(120),
      memorySize: 512,
      bundling: commonBundling,
      environment: commonEnv,
      description: "Demo-only Slack adapter: push async job completions to the originating thread.",
    });
    this.secret.grantRead(notifier);
    notifier.addToRolePolicy(invokeMainAgent);
    notifier.addToRolePolicy(readArtifacts);
    threadMap.grantReadWriteData(notifier);
    notifier.addEventSource(
      new lambdaEventSources.DynamoEventSource(props.jobRegistryTable, {
        startingPosition: lambda.StartingPosition.LATEST,
        batchSize: 10,
        retryAttempts: 2,
        bisectBatchOnError: true,
        maxRecordAge: cdk.Duration.hours(1),
      }),
    );

    new cdk.CfnOutput(this, "SlackEventsUrl", {
      value: this.eventsUrl,
      description: "Slack Events Request URL — paste into the Slack app Event Subscriptions.",
    });
    new cdk.CfnOutput(this, "SlackSecretArn", {
      value: this.secret.secretArn,
      description: "Populate JSON {signingSecret, botToken, botUserId} via put-secret-value.",
    });
  }
}
