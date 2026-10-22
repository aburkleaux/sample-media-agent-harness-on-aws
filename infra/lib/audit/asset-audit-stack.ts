/**
 * `AssetAuditStack` — the append-only audit history of every Project_Asset event (asset-lineage
 * Phase 3, requirements D6: "S3 Tables holds the append-only audit history").
 *
 * Nested stack, created ONLY where `envConfig.assetAuditTable` is set (staging today). It is purely
 * additive: it reads the EXISTING projects-table stream and writes nothing the harness reads, so an
 * env without the flag synthesizes exactly as before and turning the flag off later removes the
 * appender while the table and its history stay.
 *
 * Contents:
 *   - an S3 Tables table bucket + namespace + the `asset_events` Iceberg table (L1 `CfnTableBucket` /
 *     `CfnNamespace` / `CfnTable`; the pinned CDK 2.244 has no L2 for S3 Tables), partitioned by
 *     `project_id` and by day of `event_time` so a project's history is one partition scan;
 *   - an Athena workgroup + its results bucket (the append engine, see below);
 *   - `AssetAuditAppenderFn`: the projects-table stream, filtered to `ASSET#` rows, batched into ONE
 *     Iceberg commit per batch (`lib/lambdas/asset-audit/`), with a DLQ.
 *
 * Why Athena is the writer: S3 Tables exposes no row-level write API — an append means writing a data
 * file and committing an Iceberg snapshot, which needs an Iceberg client (PyIceberg/Spark: far too
 * heavy for a stream consumer) or a query engine. Athena's `INSERT INTO` is exactly one commit, needs
 * no bundled dependency, and is reached with SigV4 over HTTPS like the other TypeScript Lambdas here.
 *
 * ONE out-of-band step, documented rather than silently assumed: Athena reaches a table bucket only
 * through the account+region singleton federated Glue catalog `s3tablescatalog` (each table bucket
 * then appears as `s3tablescatalog/<table-bucket>`). There is no CloudFormation resource for it in
 * CDK 2.244, and it is account-wide, so it is not a resource of this stack. Once per account+region,
 * an operator creates it in IAM access-control mode (no Lake Formation grant needed):
 *
 *   aws glue create-catalog --name s3tablescatalog --catalog-input \
 *     '{"FederatedCatalog":{"Identifier":"arn:aws:s3tables:<region>:<acct>:bucket/*",
 *       "ConnectionName":"aws:s3tables"},
 *       "CreateDatabaseDefaultPermissions":[{"Principal":{"DataLakePrincipalIdentifier":"IAM_ALLOWED_PRINCIPALS"},"Permissions":["ALL"]}],
 *       "CreateTableDefaultPermissions":[{"Principal":{"DataLakePrincipalIdentifier":"IAM_ALLOWED_PRINCIPALS"},"Permissions":["ALL"]}]}'
 *
 * The appender's event-source mapping is gated by a SECOND flag, `assetAuditAppend`
 * (`appendEnabled` here), so the table can be created first and the appender only starts consuming
 * once that catalog exists. Until then (or if a statement fails) the batch goes to the DLQ, and
 * nothing else in the harness is affected.
 *
 * Case trap (the 2026-10-08 staging rollback): `AWS::S3Tables::Table` maintenance statuses are
 * LOWER case (`enabled`), while `AWS::S3Tables::TableBucket.UnreferencedFileRemoval.Status` is TITLE
 * case (`Enabled`). And IAM descriptions must be Latin-1 (no em dash). The synth test pins all three.
 */
import * as cdk from "aws-cdk-lib";
import * as athena from "aws-cdk-lib/aws-athena";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { NodejsFunction } from "aws-cdk-lib/aws-lambda-nodejs";
import { SqsDlq } from "aws-cdk-lib/aws-lambda-event-sources";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3tables from "aws-cdk-lib/aws-s3tables";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as path from "path";
import { Construct } from "constructs";

export interface AssetAuditStackProps extends cdk.NestedStackProps {
  readonly projectName: string;
  /** Harness env name — part of the (globally scoped) table-bucket name. */
  readonly envName: string;
  readonly projectsTableStreamArn: string;
  /**
   * Whether the appender consumes the stream (`envConfig.assetAuditAppend`). Off until the
   * `s3tablescatalog` integration exists, so the table can deploy first.
   */
  readonly appendEnabled?: boolean;
}

/** The Iceberg table name. One table; the event type is a column, not a table. */
export const AUDIT_TABLE_NAME = "asset_events";
/** The namespace (Athena database) inside the table bucket. */
export const AUDIT_NAMESPACE = "harness";

export function auditTableBucketName(projectName: string, envName: string): string {
  return `${projectName}-audit-${envName}`;
}

export function auditAppenderFunctionName(projectName: string): string {
  return `${projectName}-asset-audit-appender`;
}

/**
 * The `asset_events` columns, in Iceberg field-id order. Field ids are explicit because a partition
 * spec references a column by `sourceId`, and because Iceberg schema evolution is id-based: renaming a
 * column must keep its id, and a new column must take the next unused one.
 */
const SCHEMA_FIELDS: { id: number; name: string; type: string; required?: boolean }[] = [
  { id: 1, name: "event_id", type: "string", required: true },
  { id: 2, name: "event_time", type: "timestamp", required: true },
  { id: 3, name: "event_type", type: "string", required: true },
  { id: 4, name: "project_id", type: "string", required: true },
  { id: 5, name: "asset_id", type: "string", required: true },
  { id: 6, name: "status", type: "string" },
  { id: 7, name: "source", type: "string" },
  { id: 8, name: "role", type: "string" },
  { id: 9, name: "visibility", type: "string" },
  { id: 10, name: "session_id", type: "string" },
  // JSON array of asset ids. A `list<string>` column would need a typed writer; the history is read
  // by analysts and by `json_parse()` in Athena, so a JSON string costs nothing and keeps the
  // append one plain SQL statement.
  { id: 11, name: "derived_from", type: "string" },
  { id: 12, name: "checksum", type: "string" },
  { id: 13, name: "actor", type: "string" },
];

export class AssetAuditStack extends cdk.NestedStack {
  public readonly tableBucket: s3tables.CfnTableBucket;
  public readonly table: s3tables.CfnTable;
  public readonly appenderFn: lambda.Function;
  /** Appender role (IAM-mode access to the table, see the doc comment). */
  public readonly appenderRole: iam.Role;
  /** `s3tablescatalog/<table-bucket>` — the federated Glue catalog the integration creates. */
  public readonly athenaCatalog: string;

  constructor(scope: Construct, id: string, props: AssetAuditStackProps) {
    super(scope, id, props);
    const { projectName, envName } = props;
    const bucketName = auditTableBucketName(projectName, envName);
    this.athenaCatalog = `s3tablescatalog/${bucketName}`;

    // ---- S3 Tables: table bucket → namespace → the Iceberg table -------------------------------
    // RETAIN everywhere: an audit history that a stack rollback can delete is not an audit history.
    this.tableBucket = new s3tables.CfnTableBucket(this, "AuditTableBucket", {
      tableBucketName: bucketName,
      // Table-level maintenance keeps the small files this append pattern produces in check without a
      // second writer: compaction rewrites them, snapshot management expires old snapshots.
      unreferencedFileRemoval: { status: "Enabled", noncurrentDays: 10, unreferencedDays: 3 },
    });
    this.tableBucket.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    const namespace = new s3tables.CfnNamespace(this, "AuditNamespace", {
      tableBucketArn: this.tableBucket.attrTableBucketArn,
      namespace: AUDIT_NAMESPACE,
    });
    namespace.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    this.table = new s3tables.CfnTable(this, "AssetEventsTable", {
      tableBucketArn: this.tableBucket.attrTableBucketArn,
      namespace: AUDIT_NAMESPACE,
      tableName: AUDIT_TABLE_NAME,
      openTableFormat: "ICEBERG",
      icebergMetadata: {
        icebergSchema: {
          schemaFieldList: SCHEMA_FIELDS.map((f) => ({
            id: f.id, name: f.name, type: f.type, required: f.required ?? false,
          })),
        },
        // Partitioned by project first (every question starts "what happened in this project"), then
        // by day so a time window prunes rather than scans.
        icebergPartitionSpec: {
          specId: 0,
          fields: [
            { fieldId: 1000, name: "project_id", sourceId: 4, transform: "identity" },
            { fieldId: 1001, name: "event_day", sourceId: 2, transform: "day" },
          ],
        },
        tableProperties: { "write.parquet.compression-codec": "zstd" },
      },
      // LOWER case: the Table enum is [enabled, disabled] (the TableBucket one above is title case).
      compaction: { status: "enabled", targetFileSizeMb: 128 },
      snapshotManagement: { status: "enabled", maxSnapshotAgeHours: 168, minSnapshotsToKeep: 1 },
    });
    this.table.node.addDependency(namespace);
    this.table.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    // ---- Athena: the append engine --------------------------------------------------------------
    // Its own workgroup + results bucket so the audit appends never share a workgroup (and its
    // concurrency and output location) with anybody's ad-hoc queries. An `INSERT` returns no rows, so
    // the results objects are metadata only and expire quickly.
    const results = new s3.Bucket(this, "AuditQueryResults", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(7) }],
    });
    const workgroup = new athena.CfnWorkGroup(this, "AuditWorkgroup", {
      name: `${projectName}-asset-audit`,
      description: "asset_events appends (asset-lineage Phase 3). One INSERT per stream batch.",
      state: "ENABLED",
      recursiveDeleteOption: true,
      workGroupConfiguration: {
        enforceWorkGroupConfiguration: true,
        publishCloudWatchMetricsEnabled: true,
        resultConfiguration: { outputLocation: `s3://${results.bucketName}/athena/` },
        engineVersion: { selectedEngineVersion: "AUTO" },
      },
    });

    // ---- appender -------------------------------------------------------------------------------
    const appenderName = auditAppenderFunctionName(projectName);
    this.appenderRole = new iam.Role(this, "AssetAuditAppenderRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description:
        "asset_events appender: read the projects-table stream, run one Athena INSERT per batch. " +
        "No projects-table item access, no harness bucket access - it only forwards what the stream says.",
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole")],
    });
    this.appenderRole.addToPolicy(new iam.PolicyStatement({
      sid: "ReadProjectsStream",
      actions: ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"],
      resources: [props.projectsTableStreamArn],
    }));
    this.appenderRole.addToPolicy(new iam.PolicyStatement({
      sid: "RunAuditInsert",
      actions: ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetWorkGroup"],
      resources: [`arn:aws:athena:${this.region}:${this.account}:workgroup/${workgroup.name}`],
    }));
    this.appenderRole.addToPolicy(new iam.PolicyStatement({
      sid: "AuditQueryResults",
      actions: ["s3:GetBucketLocation", "s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:AbortMultipartUpload"],
      resources: [results.bucketArn, `${results.bucketArn}/*`],
    }));
    // The table itself, IAM access-control mode: the s3tables actions are what Athena uses to resolve
    // and commit the Iceberg metadata. `lakeformation:GetDataAccess` (below) is kept, harmlessly, for
    // a catalog that is later switched to Lake Formation mode.
    this.appenderRole.addToPolicy(new iam.PolicyStatement({
      sid: "AuditTableAppend",
      actions: [
        "s3tables:GetTableBucket", "s3tables:GetNamespace", "s3tables:ListNamespaces", "s3tables:ListTables", "s3tables:GetTable", "s3tables:GetTableData",
        "s3tables:PutTableData", "s3tables:GetTableMetadataLocation", "s3tables:UpdateTableMetadataLocation",
      ],
      resources: [
        this.tableBucket.attrTableBucketArn,
        `${this.tableBucket.attrTableBucketArn}/table/*`,
      ],
    }));
    this.appenderRole.addToPolicy(new iam.PolicyStatement({
      sid: "AuditCatalogRead",
      actions: ["lakeformation:GetDataAccess", "glue:GetCatalog", "glue:GetDatabase", "glue:GetDatabases",
        "glue:GetTable", "glue:GetTables", "glue:UpdateTable"],
      resources: ["*"], // the federated catalog is created out of band; its ARN is not known at synth
    }));

    const dlq = new sqs.Queue(this, "AssetAuditDlq", {
      queueName: `${projectName}-asset-audit-dlq`,
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });
    dlq.grantSendMessages(this.appenderRole);

    this.appenderFn = new NodejsFunction(this, "AssetAuditAppenderFn", {
      functionName: appenderName,
      runtime: lambda.Runtime.NODEJS_22_X,
      entry: path.join(__dirname, "..", "lambdas", "asset-audit", "handler.ts"),
      handler: "handler",
      // Bundle everything (the smithy signer): `@aws-sdk/client-athena` is not in this repo's mirror,
      // so the statement is submitted with SigV4 over HTTPS — same as the Slack Lambdas.
      bundling: { externalModules: [] },
      role: this.appenderRole,
      memorySize: 256,
      // Long enough for one Athena INSERT to commit (the handler's own poll budget ends first).
      timeout: cdk.Duration.seconds(180),
      environment: {
        ATHENA_WORKGROUP: workgroup.name,
        ATHENA_CATALOG: this.athenaCatalog,
        AUDIT_NAMESPACE: AUDIT_NAMESPACE,
        AUDIT_TABLE: AUDIT_TABLE_NAME,
        DEPLOY_ENV: envName,
      },
      description:
        "Append-only asset_events history: projects-table stream to ONE Athena INSERT (one Iceberg commit) per batch.",
      logGroup: new logs.LogGroup(this, "AssetAuditAppenderLogs", {
        logGroupName: `/aws/lambda/${appenderName}`,
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    this.appenderFn.node.addDependency(this.table);

    // Batched on purpose: up to 200 records or 60 s, whichever comes first, = one commit. The
    // filters keep it to ASSET# rows; META/QUEUE/HASH/SRC bookkeeping never reaches the function, so
    // a busy project's `changeSeq` churn costs nothing.
    new lambda.EventSourceMapping(this, "AssetAuditStream", {
      target: this.appenderFn,
      enabled: props.appendEnabled === true,
      eventSourceArn: props.projectsTableStreamArn,
      startingPosition: lambda.StartingPosition.LATEST,
      batchSize: 200,
      maxBatchingWindow: cdk.Duration.seconds(60),
      retryAttempts: 3,
      bisectBatchOnError: false, // the statement is per batch; bisecting cannot isolate a bad row
      reportBatchItemFailures: true,
      maxRecordAge: cdk.Duration.hours(6),
      onFailure: new SqsDlq(dlq),
      filters: ["INSERT", "MODIFY", "REMOVE"].map((eventName) => lambda.FilterCriteria.filter({
        eventName: lambda.FilterRule.isEqual(eventName),
        dynamodb: { Keys: { SK: { S: lambda.FilterRule.beginsWith("ASSET#") } } },
      })),
    });

    new cdk.CfnOutput(this, "AssetAuditTableArn", {
      value: this.table.attrTableArn,
      description: "The append-only asset_events Iceberg table (asset-lineage Phase 3).",
    });
    new cdk.CfnOutput(this, "AssetAuditAppenderRoleArn", {
      value: this.appenderRole.roleArn,
      description: "The asset_events appender role (IAM-mode; needs the s3tablescatalog integration, see the stack doc comment).",
    });
  }
}
