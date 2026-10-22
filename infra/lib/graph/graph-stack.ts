/**
 * Per-Project knowledge graph (nested stack). Two x86_64 container-image Lambdas built from ONE image
 * (`agent/graph.Dockerfile`, pushed to the out-of-band ECR repo `<projectName>-graph` by CI):
 *
 *   GraphWriter  `<projectName>-graph-writer`   project_graph.handler.writer_handler
 *     Projects-table stream, filtered to `SK = META` with `graph = true` on INSERT (new Projects, incl.
 *     Personal) and MODIFY (enable, rename/tags, members, every asset change bumps META `changeSeq`).
 *     Reserved concurrency 1 = the single writer per Lance table; `_meta.json` is a CAS on top of that.
 *     Also invoked async (`{op:"sync"}`) by the reader to self-heal a missed record.
 *   GraphReader  `<projectName>-graph-reader`   project_graph.handler.reader_handler
 *     Invoked ONLY by the ui proxy (resource policy names the proxy task role), after `checkAccess`.
 *
 * Why a container image on a non-AWS base: lance-graph 0.5.4 ships only a manylinux_2_39_x86_64 wheel, and
 * the AWS Lambda Python base images are Amazon Linux 2023 (glibc 2.34). Nested because the parent template
 * is near CloudFormation's 1 MB limit (`test:template-budget`).
 *
 * Data: `s3://<ingest>/projects/<pid>/graph/` — inside the Project prefix, so project purge (which deletes
 * every object version there) removes the graph with no extra step.
 */
import * as cdk from "aws-cdk-lib";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { SqsDlq } from "aws-cdk-lib/aws-lambda-event-sources";
import * as logs from "aws-cdk-lib/aws-logs";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as fs from "fs";
import * as path from "path";
import { Construct } from "constructs";
import { GRAPH_TARGET_NAME, buildGraphToolPolicy } from "./graph-policy";

export interface GraphStackProps extends cdk.NestedStackProps {
  readonly projectName: string;
  readonly projectsTableName: string;
  readonly projectsTableArn: string;
  readonly projectsTableStreamArn: string;
  readonly ingestBucketName: string;
  /** The ui proxy task role — the ONLY principal allowed to invoke the reader. */
  readonly proxyTaskRoleArn: string;
  /** ECR repository name (created out of band, like `<projectName>-ui-proxy`). */
  readonly repositoryName: string;
  /** Image tag CI pushed (`<sha>-<pipeline-iid>`). */
  readonly imageTag: string;
  /**
   * The project Search_Gateway (ProjectsStack). When given, the agent-facing graph tools are added to it as a
   * Lambda target with one Cedar permit per tool on its ENFORCE engine — privacy exactly like search.
   */
  readonly search?: {
    readonly gatewayId: string;
    readonly gatewayArn: string;
    readonly gatewayRoleArn: string;
    readonly policyEngineId: string;
  };
}

/** Agent-facing graph tools: `agent/src/project_graph/tool_schema.json` (generated from `tools.py`, test-pinned). */
export function graphToolSpecs(): { name: string; description: string; inputSchema: Record<string, unknown> }[] {
  const file = path.join(__dirname, "..", "..", "..", "agent", "src", "project_graph", "tool_schema.json");
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

export function graphToolsFunctionName(projectName: string): string {
  return `${projectName}-graph-tools`;
}

export function graphReaderFunctionName(projectName: string): string {
  return `${projectName}-graph-reader`;
}

export function graphWriterFunctionName(projectName: string): string {
  return `${projectName}-graph-writer`;
}

export function graphRepositoryName(projectName: string): string {
  return `${projectName}-graph`;
}

export class GraphStack extends cdk.NestedStack {
  public readonly readerFn: lambda.DockerImageFunction;
  public readonly writerFn: lambda.DockerImageFunction;
  public readonly toolsFn?: lambda.DockerImageFunction;

  constructor(scope: Construct, id: string, props: GraphStackProps) {
    super(scope, id, props);
    const { projectName } = props;
    const bucketArn = `arn:aws:s3:::${props.ingestBucketName}`;
    const graphObjects = `${bucketArn}/projects/*/graph/*`;
    const repo = ecr.Repository.fromRepositoryName(this, "GraphRepo", props.repositoryName);
    const writerName = graphWriterFunctionName(projectName);
    const readerName = graphReaderFunctionName(projectName);

    const env = {
      PROJECTS_TABLE: props.projectsTableName,
      INGEST_BUCKET: props.ingestBucketName,
      GRAPH_QUERY_ENGINE: "cypher",
      RUST_LOG: "error",
    };

    // ---- writer ---------------------------------------------------------------------------------
    const writerRole = new iam.Role(this, "GraphWriterRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole")],
    });
    writerRole.addToPolicy(new iam.PolicyStatement({
      sid: "GraphObjects", actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], resources: [graphObjects],
    }));
    writerRole.addToPolicy(new iam.PolicyStatement({
      sid: "ListGraphPrefix", actions: ["s3:ListBucket"], resources: [bucketArn],
      conditions: { StringLike: { "s3:prefix": ["projects/*/graph/*"] } },
    }));
    writerRole.addToPolicy(new iam.PolicyStatement({
      sid: "ProjectRows", actions: ["dynamodb:GetItem", "dynamodb:Query"], resources: [props.projectsTableArn],
    }));

    const dlq = new sqs.Queue(this, "GraphWriterDlq", {
      queueName: `${projectName}-graph-writer-dlq`, retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });

    this.writerFn = new lambda.DockerImageFunction(this, "GraphWriterFn", {
      functionName: writerName,
      description: "Knowledge graph writer: sync a Project's Lance graph from its DynamoDB rows (single writer).",
      code: lambda.DockerImageCode.fromEcr(repo, { tagOrDigest: props.imageTag, cmd: ["project_graph.handler.writer_handler"] }),
      architecture: lambda.Architecture.X86_64,
      memorySize: 2048, timeout: cdk.Duration.seconds(300), ephemeralStorageSize: cdk.Size.gibibytes(1),
      reservedConcurrentExecutions: 1,
      role: writerRole, environment: env,
      logGroup: new logs.LogGroup(this, "GraphWriterLogs", {
        logGroupName: `/aws/lambda/${writerName}`, retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    new lambda.EventSourceMapping(this, "GraphWriterStream", {
      target: this.writerFn, eventSourceArn: props.projectsTableStreamArn,
      startingPosition: lambda.StartingPosition.LATEST, batchSize: 100, maxBatchingWindow: cdk.Duration.seconds(10),
      retryAttempts: 3, bisectBatchOnError: true, reportBatchItemFailures: true,
      maxRecordAge: cdk.Duration.hours(6),
      onFailure: new SqsDlq(dlq),
      filters: ["INSERT", "MODIFY"].map((eventName) => lambda.FilterCriteria.filter({
        eventName: lambda.FilterRule.isEqual(eventName),
        dynamodb: { Keys: { SK: { S: lambda.FilterRule.isEqual("META") } }, NewImage: { graph: { BOOL: [true] } } },
      })),
    });
    // The event source mapping polls the stream with the function's role.
    writerRole.addToPolicy(new iam.PolicyStatement({
      sid: "ReadProjectsStream",
      actions: ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"],
      resources: [props.projectsTableStreamArn],
    }));
    dlq.grantSendMessages(writerRole);

    // ---- reader ---------------------------------------------------------------------------------
    const readerRole = new iam.Role(this, "GraphReaderRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole")],
    });
    readerRole.addToPolicy(new iam.PolicyStatement({ sid: "GraphObjects", actions: ["s3:GetObject"], resources: [graphObjects] }));
    readerRole.addToPolicy(new iam.PolicyStatement({
      sid: "ListGraphPrefix", actions: ["s3:ListBucket"], resources: [bucketArn],
      conditions: { StringLike: { "s3:prefix": ["projects/*/graph/*"] } },
    }));
    readerRole.addToPolicy(new iam.PolicyStatement({ sid: "ProjectMeta", actions: ["dynamodb:GetItem"], resources: [props.projectsTableArn] }));
    readerRole.addToPolicy(new iam.PolicyStatement({
      sid: "RequestSync", actions: ["lambda:InvokeFunction"],
      resources: [`arn:aws:lambda:${this.region}:${this.account}:function:${writerName}`],
    }));

    this.readerFn = new lambda.DockerImageFunction(this, "GraphReaderFn", {
      functionName: readerName,
      description: "Knowledge graph reader: overview / neighbourhood / stats for the ui proxy (lance-graph Cypher).",
      code: lambda.DockerImageCode.fromEcr(repo, { tagOrDigest: props.imageTag, cmd: ["project_graph.handler.reader_handler"] }),
      architecture: lambda.Architecture.X86_64,
      // 60 s = the CloudFront read timeout and the ALB idle timeout on the proxy path; the proxy's own budget
      // (GRAPH_SERVICE_BUDGET_MS, 58 s) ends first so the caller still gets a JSON 503, not a CloudFront 504.
      memorySize: 2048, timeout: cdk.Duration.seconds(60),
      role: readerRole, environment: { ...env, GRAPH_WRITER_FN: writerName },
      logGroup: new logs.LogGroup(this, "GraphReaderLogs", {
        logGroupName: `/aws/lambda/${readerName}`, retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    this.readerFn.addPermission("ProxyInvoke", {
      principal: new iam.ArnPrincipal(props.proxyTaskRoleArn), action: "lambda:InvokeFunction",
    });

    if (props.search) this.addSearchGatewayTools(props, repo, env, readerRole, writerName);
  }

  /**
   * Agent-facing graph tools on the project Search_Gateway: a Lambda target `project-graph` (inline schema,
   * GATEWAY_IAM_ROLE) over a third function from the same image, and one ENFORCE-mode Cedar permit per tool
   * requiring `context.input.project_id == principal.getTag("project_id")`. Read-only; same read IAM as the reader.
   */
  private addSearchGatewayTools(props: GraphStackProps, repo: ecr.IRepository, env: Record<string, string>,
    readRole: iam.Role, writerName: string): void {
    const search = props.search!;
    const specs = graphToolSpecs();
    const toolsName = graphToolsFunctionName(props.projectName);
    const toolsFn = new lambda.DockerImageFunction(this, "GraphToolsFn", {
      functionName: toolsName,
      description: "Knowledge graph tools for agents (Search_Gateway Lambda target `project-graph`, Cedar-scoped per Project).",
      code: lambda.DockerImageCode.fromEcr(repo, { tagOrDigest: props.imageTag, cmd: ["project_graph.handler.tool_handler"] }),
      architecture: lambda.Architecture.X86_64,
      memorySize: 2048, timeout: cdk.Duration.seconds(30),
      role: readRole, environment: { ...env, GRAPH_WRITER_FN: writerName },
      logGroup: new logs.LogGroup(this, "GraphToolsLogs", {
        logGroupName: `/aws/lambda/${toolsName}`, retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    (this as { toolsFn?: lambda.DockerImageFunction }).toolsFn = toolsFn;

    // The gateway invokes the target with its own service role (GATEWAY_IAM_ROLE).
    const gatewayRole = iam.Role.fromRoleArn(this, "SearchGatewayRole", search.gatewayRoleArn, { mutable: true });
    gatewayRole.attachInlinePolicy(new iam.Policy(this, "SearchGatewayInvokeGraphTools", {
      statements: [new iam.PolicyStatement({ actions: ["lambda:InvokeFunction"], resources: [toolsFn.functionArn] })],
    }));

    const target = new cdk.aws_bedrockagentcore.CfnGatewayTarget(this, "GraphToolsTarget", {
      gatewayIdentifier: search.gatewayId,
      name: GRAPH_TARGET_NAME,
      description: "Per-Project knowledge graph (read-only), the graph extension of project search.",
      targetConfiguration: { mcp: { lambda: { lambdaArn: toolsFn.functionArn, toolSchema: { inlinePayload: specs as never } } } },
      credentialProviderConfigurations: [{ credentialProviderType: "GATEWAY_IAM_ROLE" }],
    });
    target.node.addDependency(gatewayRole);

    const prefix = props.projectName.replace(/[^A-Za-z0-9]/g, "_");
    for (const spec of specs) {
      const policy = new cdk.aws_bedrockagentcore.CfnPolicy(this, `GraphPermit_${spec.name}`, {
        name: `${prefix}_permit_${spec.name}`,
        policyEngineId: search.policyEngineId,
        definition: { cedar: { statement: buildGraphToolPolicy(spec.name, search.gatewayArn) } },
        // Safe under FAIL_ON_ANY_FINDINGS: the policy depends on the target, so the action is in the
        // gateway's schema before it validates (same reasoning as the search permit).
        validationMode: "FAIL_ON_ANY_FINDINGS",
      });
      policy.addDependency(target);
    }
  }
}
