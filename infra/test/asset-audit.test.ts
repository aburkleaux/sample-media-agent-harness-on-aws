/**
 * Unit tests for the `asset_events` appender's mapping + batching (`lib/lambdas/asset-audit/`).
 * Run: `npm run test:asset-audit` (node:assert, no framework — like the other infra tests).
 *
 * What matters here and is easy to get wrong: which stream records become audit events at all (a
 * `rowVersion` bump is not an event), that a batch becomes ONE statement (a commit per row is the
 * documented S3 Tables cost trap), and that nothing in a display name or session id can break out of
 * a SQL string literal.
 */
import * as assert from "node:assert/strict";
import {
  COLUMNS, MAX_ROWS, batchStatements, buildInsertSql, sqlString, toAuditEvent, toAuditEvents,
  type StreamRecord,
} from "../lib/lambdas/asset-audit/auditEvent";

const PID = "p" + "a".repeat(32);
const AID = "video:0b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8";
const TABLE = '"s3tablescatalog/harness-audit-staging"."harness"."asset_events"';

/** An ASSET row as the stream delivers it (attribute-value shapes). */
function assetImage(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    assetId: { S: AID },
    kind: { S: "video" },
    source: { S: "session" },
    role: { S: "deliverable" },
    visibility: { S: "project" },
    sessionId: { S: "sess-1" },
    status: { S: "stored" },
    registeredAt: { S: "2026-10-08T12:00:00Z" },
    statusChangedAt: { S: "2026-10-08T12:00:00Z" },
    addedBy: { S: "cognito-sub-1" },
    derivedFrom: { L: [{ S: "video:src-1" }, { S: "audio:src-2" }] },
    checksum: { M: { algo: { S: "CRC64NVME" }, value: { S: "abc==" }, size: { N: "1234" } } },
    ...over,
  };
}

function record(
  eventName: string,
  images: { NewImage?: Record<string, unknown>; OldImage?: Record<string, unknown> },
  over: Partial<StreamRecord> = {},
): StreamRecord {
  return {
    eventName,
    eventID: "e1",
    dynamodb: {
      Keys: { PK: { S: `PROJECT#${PID}` }, SK: { S: `ASSET#${AID}` } },
      ApproximateCreationDateTime: 1_791_000_000,
      ...images,
    },
    ...over,
  };
}

// ---- which records become events ---------------------------------------------------------------
{
  const reg = toAuditEvent(record("INSERT", { NewImage: assetImage() }));
  assert.ok(reg, "an ASSET INSERT is a registration");
  assert.equal(reg!.eventType, "registered");
  assert.equal(reg!.projectId, PID, "PROJECT# is stripped from the partition key");
  assert.equal(reg!.assetId, AID);
  assert.equal(reg!.source, "session");
  assert.equal(reg!.role, "deliverable");
  assert.equal(reg!.visibility, "project");
  assert.equal(reg!.sessionId, "sess-1");
  assert.equal(reg!.actor, "cognito-sub-1");
  assert.equal(reg!.derivedFrom, '["video:src-1","audio:src-2"]', "lineage travels as JSON");
  assert.equal(reg!.checksum, "CRC64NVME:abc==:1234");
  assert.equal(reg!.eventTime, "2026-10-08 12:00:00.000", "the row's own registeredAt, not the clock");
  assert.equal(reg!.eventId, "e1:registered");

  // A status transition.
  const indexed = toAuditEvent(record("MODIFY", {
    OldImage: assetImage({ status: { S: "queued" } }),
    NewImage: assetImage({ status: { S: "indexed" }, statusChangedAt: { S: "2026-10-08T12:05:30Z" } }),
  }));
  assert.equal(indexed?.eventType, "status");
  assert.equal(indexed?.status, "indexed");
  assert.equal(indexed?.eventTime, "2026-10-08 12:05:30.000");

  // A removal is its own type, and its actor is whoever removed it.
  const removed = toAuditEvent(record("MODIFY", {
    OldImage: assetImage({ status: { S: "indexed" } }),
    NewImage: assetImage({
      status: { S: "removed" }, removedAt: { S: "2026-10-08T13:00:00Z" }, removedBy: { S: "cognito-sub-2" },
    }),
  }));
  assert.equal(removed?.eventType, "removed");
  assert.equal(removed?.actor, "cognito-sub-2");
  assert.equal(removed?.eventTime, "2026-10-08 13:00:00.000");

  // The row itself disappearing is a purge, described from the OLD image.
  const purged = toAuditEvent(record("REMOVE", { OldImage: assetImage({ status: { S: "removed" } }) }));
  assert.equal(purged?.eventType, "purged");
  assert.equal(purged?.assetId, AID);
  assert.equal(purged?.status, "removed");

  // A MODIFY that does not change the status is bookkeeping (rowVersion, queue cursors): no event.
  assert.equal(
    toAuditEvent(record("MODIFY", {
      OldImage: assetImage({ rowVersion: { N: "1" } }),
      NewImage: assetImage({ rowVersion: { N: "2" } }),
    })),
    null,
    "a rowVersion bump is not an audit event",
  );

  // Non-ASSET rows never reach the table (the stream filter agrees, but the mapper does not trust it).
  for (const sk of ["META", "QUEUE#1", "HASH#CRC64NVME:x:1", `SRC#${"a".repeat(64)}`, "JOB#j1"]) {
    const r = record("INSERT", { NewImage: assetImage() });
    r.dynamodb!.Keys!.SK = { S: sk };
    assert.equal(toAuditEvent(r), null, `${sk} is not an asset event`);
  }

  // An asset with none of the lineage attributes (a legacy upload) still audits, with empty columns —
  // absent is recorded as absent, never invented.
  const legacy = toAuditEvent(record("INSERT", {
    NewImage: { assetId: { S: AID }, status: { S: "queued" }, source: { S: "upload" } },
  }));
  assert.equal(legacy?.role, "");
  assert.equal(legacy?.visibility, "");
  assert.equal(legacy?.derivedFrom, "");
  assert.equal(legacy?.checksum, "");
  // No row timestamp: the record's own creation time (1791000000s), not the appender's clock.
  assert.equal(legacy?.eventTime, "2026-10-03 04:00:00.000");

  // `sessionId` may only be on the producer map (an m2c output).
  const viaProducer = toAuditEvent(record("INSERT", {
    NewImage: assetImage({
      sessionId: { S: "" },
      producer: { M: { sessionId: { S: "sess-9" }, jobId: { S: "job-1" } } },
    }),
  }));
  assert.equal(viaProducer?.sessionId, "sess-9");

  // A malformed row timestamp falls back instead of emitting `TIMESTAMP ''`, which would fail the
  // statement for the whole batch (event_time is required and partitioned).
  const malformed = toAuditEvent(record("INSERT", {
    NewImage: assetImage({ registeredAt: { S: "2026-13-45T99:99:99Z" } }),
  }));
  assert.equal(malformed?.eventTime, "2026-10-03 04:00:00.000");

  assert.equal(toAuditEvents([record("INSERT", { NewImage: assetImage() }),
    record("MODIFY", { OldImage: assetImage(), NewImage: assetImage() })]).length, 1,
    "a batch keeps only the records that are events");
  console.log("asset-audit mapping: ok");
}

// ---- one statement per batch --------------------------------------------------------------------
{
  const events = toAuditEvents(Array.from({ length: 50 }, (_, i) =>
    record("INSERT", { NewImage: assetImage({ assetId: { S: `video:asset-${i}` } }) }, { eventID: `e${i}` })));
  assert.equal(events.length, 50);
  const statements = batchStatements(TABLE, events);
  assert.equal(statements.length, 1, "50 records = ONE Iceberg commit, not 50");
  assert.ok(statements[0].startsWith(`INSERT INTO ${TABLE} (${COLUMNS.join(", ")}) VALUES (`));
  assert.equal(statements[0].match(/TIMESTAMP '/g)?.length, 50, "one VALUES row per event");
  assert.ok(!statements[0].includes("INSERT INTO", 20), "a single statement, not concatenated inserts");

  // Only an over-long batch splits, and then into as few statements as possible.
  const many = toAuditEvents(Array.from({ length: MAX_ROWS + 10 }, (_, i) =>
    record("INSERT", { NewImage: assetImage({ assetId: { S: `video:asset-${i}` } }) }, { eventID: `e${i}` })));
  const split = batchStatements(TABLE, many);
  assert.equal(split.length, 2, `${MAX_ROWS + 10} events split into 2 statements`);
  assert.equal(split[0].match(/TIMESTAMP '/g)?.length, MAX_ROWS);
  assert.equal(split[1].match(/TIMESTAMP '/g)?.length, 10);

  assert.deepEqual(batchStatements(TABLE, []), [], "an empty batch runs no statement");
  assert.throws(() => buildInsertSql(TABLE, []), /no events/);
  console.log("asset-audit batching: ok");
}

// ---- SQL literals -------------------------------------------------------------------------------
{
  assert.equal(sqlString("plain"), "'plain'");
  assert.equal(sqlString("O'Brien"), "'O''Brien'");
  assert.equal(sqlString("'); DROP TABLE asset_events; --"), "'''); DROP TABLE asset_events; --'");
  const nasty = toAuditEvent(record("INSERT", {
    NewImage: assetImage({ addedBy: { S: "a' OR '1'='1" }, sessionId: { S: "x'); --" } }),
  }));
  const sql = buildInsertSql(TABLE, [nasty!]);
  assert.ok(sql.includes("'a'' OR ''1''=''1'"), "an actor id cannot close its literal");
  assert.ok(sql.includes("'x''); --'"));
  assert.equal(sql.match(/VALUES/g)?.length, 1);
  console.log("asset-audit sql escaping: ok");
}

console.log("asset-audit: all assertions passed");
