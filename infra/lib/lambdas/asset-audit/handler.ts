/**
 * `asset_events` appender: projects-table stream → ONE Iceberg commit per batch (asset-lineage
 * Phase 3, D6 "S3 Tables holds the append-only audit history").
 *
 * Append path: Athena `INSERT INTO` against the S3 Tables catalog. S3 Tables has no row-level write
 * API — appending to an Iceberg table means writing a data file AND committing a snapshot, which in
 * practice needs an Iceberg client (PySpark / PyIceberg, far too heavy for a stream consumer) or a
 * query engine. Athena is the engine AWS ships for exactly this, each `INSERT` is one commit, and it
 * needs no bundled dependency: the statement is submitted with SigV4 over HTTPS (`../slack-shared/aws`),
 * the same way the Slack Lambdas call AWS without an `@aws-sdk/client-*` the npm mirror lacks.
 *
 * ONE COMMIT PER BATCH, never per row — see `auditEvent.ts` for why. The mapping and the statement
 * building live there and are unit-tested; this file is the AWS plumbing.
 *
 * Failure handling: a failed statement returns the batch's records as `batchItemFailures`, so the
 * event-source mapping retries them and the DLQ catches a poison batch. The table is append-only, so
 * a retry can duplicate a row; `event_id` is stable per stream record, which makes a duplicate
 * identifiable (`SELECT … GROUP BY event_id`) rather than silently wrong.
 */
import { sigv4Fetch } from "../slack-shared/aws";
import { batchStatements, toAuditEvents, type StreamRecord } from "./auditEvent";

const REGION = process.env.AWS_REGION ?? "us-west-2";
const WORKGROUP = process.env.ATHENA_WORKGROUP ?? "";
const CATALOG = process.env.ATHENA_CATALOG ?? "";
const NAMESPACE = process.env.AUDIT_NAMESPACE ?? "";
const TABLE = process.env.AUDIT_TABLE ?? "asset_events";
/** How long to wait for a statement before giving the batch back to the stream. */
const POLL_TIMEOUT_MS = Number(process.env.ATHENA_TIMEOUT_MS ?? 120_000);
const POLL_INTERVAL_MS = 1_000;

const qualifiedTable = (): string => `"${CATALOG}"."${NAMESPACE}"."${TABLE}"`;

interface AthenaResponse {
  QueryExecutionId?: string;
  QueryExecution?: { Status?: { State?: string; StateChangeReason?: string } };
}

async function athena(target: string, body: unknown): Promise<AthenaResponse> {
  const r = await sigv4Fetch({
    service: "athena",
    region: REGION,
    url: `https://athena.${REGION}.amazonaws.com/`,
    headers: { "content-type": "application/x-amz-json-1.1", "x-amz-target": `AmazonAthena.${target}` },
    body: JSON.stringify(body),
  });
  if (r.status >= 300) throw new Error(`${target} ${r.status}: ${r.text.slice(0, 300)}`);
  return JSON.parse(r.text || "{}") as AthenaResponse;
}

const sleep = (ms: number): Promise<void> => new Promise((res) => setTimeout(res, ms));

/** Submit one statement and wait for it to commit. Throws with Athena's own reason on failure. */
export async function runStatement(sql: string): Promise<string> {
  const started = await athena("StartQueryExecution", {
    QueryString: sql,
    WorkGroup: WORKGROUP,
    QueryExecutionContext: { Catalog: CATALOG, Database: NAMESPACE },
  });
  const id = started.QueryExecutionId;
  if (!id) throw new Error("StartQueryExecution returned no QueryExecutionId");
  const deadline = Date.now() + POLL_TIMEOUT_MS;
  for (;;) {
    const got = await athena("GetQueryExecution", { QueryExecutionId: id });
    const status = got.QueryExecution?.Status ?? {};
    if (status.State === "SUCCEEDED") return id;
    if (status.State === "FAILED" || status.State === "CANCELLED") {
      throw new Error(`asset_events INSERT ${status.State}: ${status.StateChangeReason ?? ""} (${id})`);
    }
    if (Date.now() > deadline) throw new Error(`asset_events INSERT still ${status.State} after timeout (${id})`);
    await sleep(POLL_INTERVAL_MS);
  }
}

export async function handler(event: { Records?: StreamRecord[] }): Promise<{
  batchItemFailures: { itemIdentifier: string }[];
}> {
  const records = event.Records ?? [];
  const events = toAuditEvents(records);
  if (events.length === 0) {
    console.log(JSON.stringify({ msg: "asset-audit: nothing to append", records: records.length }));
    return { batchItemFailures: [] };
  }
  const statements = batchStatements(qualifiedTable(), events);
  try {
    for (const sql of statements) await runStatement(sql);
  } catch (err) {
    console.error(JSON.stringify({
      msg: "asset-audit: append failed", records: records.length, events: events.length,
      statements: statements.length, error: String(err),
    }));
    // Give the WHOLE batch back: the statements are per-batch, so a partial retry cannot be expressed
    // per record, and a duplicate row is recoverable where a lost audit event is not.
    return {
      batchItemFailures: records
        .map((r) => r.eventID)
        .filter((id): id is string => typeof id === "string" && id.length > 0)
        .map((itemIdentifier) => ({ itemIdentifier })),
    };
  }
  console.log(JSON.stringify({
    msg: "asset-audit: appended", records: records.length, events: events.length, commits: statements.length,
  }));
  return { batchItemFailures: [] };
}
