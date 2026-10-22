/**
 * Synth assertions over the append-only `asset_events` audit stack (asset-lineage Phase 3, D6).
 * Run: `npm run test:asset-audit-synth` (node:assert, no framework — like the other synth tests).
 *
 *  - with `assetAuditTable` forced on: the nested stack exists with ONE S3 Tables table bucket,
 *    namespace and Iceberg `asset_events` table, partitioned by project and day, RETAINed; the
 *    appender is subscribed to the PROJECTS table stream, filtered to `ASSET#` rows, batched (one
 *    commit per batch) with a DLQ and no bisect; its role can run the audit workgroup's queries and
 *    touch nothing else — no projects-table item access, no harness bucket.
 *  - the CloudFormation property validation that rolled back staging on 2026-10-08 (read with
 *    `describe-events --operation-id`): the Table's Compaction / SnapshotManagement `Status` is
 *    LOWER case, the TableBucket's UnreferencedFileRemoval `Status` TITLE case, and every IAM role
 *    description is Latin-1 (IAM rejects an em dash);
 *  - the appender mapping is DISABLED unless `assetAuditAppend` (the catalog integration comes first);
 *  - staging (from its real config) has the stack; prod and sandbox (flag absent) have nothing.
 */
import * as assert from "node:assert/strict";
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AgentInfraStack } from "../lib/agent-infra-stack";
import { resolveEnvConfig, EnvName } from "../lib/env-config";
import { AUDIT_NAMESPACE, AUDIT_TABLE_NAME } from "../lib/audit/asset-audit-stack";

const PROJECT = "mediaworks-harness";

function build(env: EnvName, overrides: Record<string, unknown> = {}) {
  const app = new cdk.App({ context: { env, projectName: PROJECT } });
  const envConfig = { ...resolveEnvConfig(app), ...overrides };
  const stack = new AgentInfraStack(app, `${PROJECT}-infra-${env}`, {
    projectName: PROJECT, envConfig, env: { account: envConfig.account, region: envConfig.region },
  });
  return {
    stack,
    parent: Template.fromStack(stack),
    audit: stack.assetAuditStack ? Template.fromStack(stack.assetAuditStack) : undefined,
  };
}

const props = (t: Template, type: string): any[] =>
  Object.values(t.findResources(type)).map((r: any) => r.Properties ?? {});
const resourcesOf = (t: Template, type: string): [string, any][] =>
  Object.entries(t.findResources(type)) as [string, any][];

// ---- staging: the audit history exists and is append-only --------------------------------------
{
  const { stack, audit } = build("staging");
  assert.ok(audit, "staging: AssetAuditStack exists");

  // ---- the table ----
  const buckets = resourcesOf(audit!, "AWS::S3Tables::TableBucket");
  assert.equal(buckets.length, 1, "one table bucket");
  assert.equal(buckets[0][1].Properties.TableBucketName, `${PROJECT}-audit-staging`);
  assert.equal(buckets[0][1].DeletionPolicy, "Retain", "an audit history a rollback can delete is not one");

  const namespaces = resourcesOf(audit!, "AWS::S3Tables::Namespace");
  assert.equal(namespaces.length, 1, "one namespace");
  assert.deepEqual(namespaces[0][1].Properties.Namespace, AUDIT_NAMESPACE);
  assert.equal(namespaces[0][1].DeletionPolicy, "Retain");

  const tables = resourcesOf(audit!, "AWS::S3Tables::Table");
  assert.equal(tables.length, 1, "one Iceberg table");
  const table = tables[0][1];
  assert.equal(table.DeletionPolicy, "Retain");
  assert.equal(table.Properties.TableName, AUDIT_TABLE_NAME);
  assert.equal(table.Properties.OpenTableFormat, "ICEBERG");
  const fields = table.Properties.IcebergMetadata.IcebergSchema.SchemaFieldList;
  assert.deepEqual(
    fields.map((f: any) => f.Name),
    ["event_id", "event_time", "event_type", "project_id", "asset_id", "status", "source", "role",
      "visibility", "session_id", "derived_from", "checksum", "actor"],
    "every recorded fact has a column (requirements: time, project, asset, type, source, role, " +
      "visibility, sessionId, derivedFrom, checksum, actor)",
  );
  // Field ids are stable and unique — a partition spec and Iceberg evolution both key off them.
  assert.deepEqual(fields.map((f: any) => f.Id), fields.map((_: any, i: number) => i + 1));
  const partition = table.Properties.IcebergMetadata.IcebergPartitionSpec.Fields;
  const byProject = partition.find((f: any) => f.Name === "project_id");
  assert.ok(byProject, "partitioned by project");
  assert.equal(byProject.Transform, "identity");
  assert.equal(byProject.SourceId, fields.find((f: any) => f.Name === "project_id").Id);
  const byDay = partition.find((f: any) => f.Transform === "day");
  assert.ok(byDay, "and by day of the event time");
  assert.equal(byDay.SourceId, fields.find((f: any) => f.Name === "event_time").Id);
  // CloudFormation's enum for AWS::S3Tables::Table is [enabled, disabled] — "Enabled" was the
  // 2026-10-08 PROPERTY_VALIDATION failure. The TableBucket's enum is the opposite case.
  assert.equal(table.Properties.Compaction?.Status, "enabled", "compaction handles the small files appends make");
  assert.equal(table.Properties.SnapshotManagement?.Status, "enabled", "snapshot management, lower case");
  assert.equal(buckets[0][1].Properties.UnreferencedFileRemoval?.Status, "Enabled",
    "the TableBucket enum is TITLE case [Enabled, Disabled]");
  for (const [id, role] of resourcesOf(audit!, "AWS::IAM::Role")) {
    const d = role.Properties.Description;
    if (d === undefined) continue;
    assert.ok(typeof d === "string" && /^[\t\n\r\x20-\x7E\xA1-\xFF]*$/.test(d),
      `${id}: IAM role description must match [\\u0009\\u000A\\u000D\\u0020-\\u007E\\u00A1-\\u00FF]* (got ${JSON.stringify(d)})`);
  }

  // ---- the appender ----
  const fns = props(audit!, "AWS::Lambda::Function").filter((f) => f.FunctionName);
  assert.equal(fns.length, 1, "one named function (the appender)");
  const fn = fns[0];
  assert.equal(fn.FunctionName, `${PROJECT}-asset-audit-appender`);
  assert.equal(fn.Runtime, "nodejs22.x");
  assert.equal(fn.Environment.Variables.AUDIT_TABLE, AUDIT_TABLE_NAME);
  assert.equal(fn.Environment.Variables.AUDIT_NAMESPACE, AUDIT_NAMESPACE);
  assert.equal(fn.Environment.Variables.ATHENA_CATALOG, `s3tablescatalog/${PROJECT}-audit-staging`);
  assert.equal(fn.Environment.Variables.ATHENA_WORKGROUP, `${PROJECT}-asset-audit`);

  const esm = props(audit!, "AWS::Lambda::EventSourceMapping");
  assert.equal(esm.length, 1, "one stream mapping");
  const mapping = esm[0];
  assert.equal(
    JSON.stringify(mapping.EventSourceArn),
    JSON.stringify({ Ref: Object.keys(audit!.findParameters("*")).find((p) => /referencetomediaworks.*ProjectsTable.*StreamArn/i.test(p)) }),
    "the EXISTING projects-table stream, passed in as a nested-stack parameter",
  );
  assert.equal(mapping.StartingPosition, "LATEST");
  assert.ok(mapping.BatchSize >= 100, `batched (one Iceberg commit per batch), got BatchSize=${mapping.BatchSize}`);
  assert.ok(mapping.MaximumBatchingWindowInSeconds >= 10, "and a batching window, so commits are not per record");
  assert.notEqual(mapping.BisectBatchOnFunctionError, true, "no bisect: the statement is per batch");
  assert.deepEqual(mapping.FunctionResponseTypes, ["ReportBatchItemFailures"]);
  assert.equal(mapping.Enabled, false, "appender disabled until assetAuditAppend (catalog integration first)");
  assert.ok(mapping.DestinationConfig?.OnFailure?.Destination, "failed batches go to a DLQ");
  const patterns = mapping.FilterCriteria.Filters.map((f: any) => JSON.parse(f.Pattern));
  assert.deepEqual(patterns.map((p: any) => p.eventName).sort(), [["INSERT"], ["MODIFY"], ["REMOVE"]],
    "registrations, status changes/removals, and purges");
  for (const p of patterns) {
    assert.deepEqual(p.dynamodb.Keys.SK.S, [{ prefix: "ASSET#" }], "ASSET# rows only — META churn never reaches it");
  }

  // ---- the appender's privileges ----
  const statements = props(audit!, "AWS::IAM::Policy").flatMap((p) => p.PolicyDocument.Statement);
  const all = JSON.stringify(statements);
  const actions = statements.flatMap((s: any) => (Array.isArray(s.Action) ? s.Action : [s.Action]));
  for (const forbidden of ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem"]) {
    assert.ok(!actions.includes(forbidden), `the appender has no projects-table item access (${forbidden})`);
  }
  assert.ok(!all.includes("skills-store"), "and no harness media-bucket access");
  assert.ok(actions.includes("athena:StartQueryExecution"), "it can submit the INSERT");
  assert.ok(actions.includes("s3tables:PutTableData"), "it can append to the table");
  assert.ok(actions.includes("dynamodb:GetRecords"), "and read the stream");
  const athenaStatement = statements.find((s: any) =>
    (Array.isArray(s.Action) ? s.Action : [s.Action]).includes("athena:StartQueryExecution"));
  assert.ok(JSON.stringify(athenaStatement.Resource).includes(`workgroup/${PROJECT}-asset-audit`),
    "scoped to the audit workgroup, not every workgroup");

  // The parent stack gained one nested stack and nothing else about the audit.
  assert.ok(stack.assetAuditStack, "exposed for this test");
  console.log("staging: asset_events table + batching appender off the projects stream — ok");
}

// ---- the append flag turns the mapping on ------------------------------------------------------
{
  const { audit } = build("staging", { assetAuditAppend: true });
  const esm = props(audit!, "AWS::Lambda::EventSourceMapping");
  assert.equal(esm.length, 1);
  assert.equal(esm[0].Enabled, true, "assetAuditAppend: true enables the appender mapping");
  console.log("staging + assetAuditAppend: appender mapping enabled — ok");
}

// ---- prod and sandbox: flag absent, nothing synthesized -----------------------------------------
for (const env of ["prod", "sandbox"] as EnvName[]) {
  const { stack, parent, audit } = build(env);
  assert.equal(audit, undefined, `${env}: no AssetAuditStack`);
  assert.equal(stack.assetAuditStack, undefined, `${env}: no audit stack exposed`);
  const json = JSON.stringify(parent.toJSON());
  assert.ok(!json.includes("AssetAudit"), `${env}: no audit resources in the template`);
  console.log(`${env}: audit history off (flag absent) — template unchanged`);
}

console.log("asset-audit-stack.synth: all assertions passed");
