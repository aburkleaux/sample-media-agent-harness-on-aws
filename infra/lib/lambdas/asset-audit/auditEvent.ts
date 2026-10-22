/**
 * Pure mapping + batching for the `asset_events` audit history (asset-lineage Phase 3, D6).
 *
 * The projects-table stream is the source of truth for what happened to a Project_Asset; this module
 * turns a stream batch into the audit rows and into ONE Iceberg-appending SQL statement per batch.
 * Separated from `handler.ts` so the interesting half (which records become which rows, how a batch
 * becomes a statement, what gets escaped) is unit-testable with no AWS at all.
 *
 * Why one statement per batch and never per row: each `INSERT INTO` on an Iceberg table is a commit —
 * a new manifest and snapshot. A commit per stream record would multiply tiny data files and
 * (with several shards) lose commits to optimistic-concurrency retries, which is the documented S3
 * Tables cost/contention trap. The event-source mapping batches (`maxBatchingWindow`), this module
 * folds the whole batch into one statement, and only an over-long statement is split (`MAX_ROWS`,
 * `MAX_SQL_BYTES` — Athena's own query-length limit).
 */

/** What happened to the asset. A MODIFY that changes nothing we audit produces no event at all. */
export type AuditEventType = "registered" | "status" | "removed" | "purged";

export interface AuditEvent {
  /** Stable per (asset, type, stream record): a redelivered record yields the same id. */
  eventId: string;
  /** ISO-8601 UTC, from the record's own timestamps (never "now"). */
  eventTime: string;
  eventType: AuditEventType;
  projectId: string;
  assetId: string;
  /** The asset status AFTER the event (`removed` for a removal, the last known for a purge). */
  status: string;
  source: string;
  role: string;
  visibility: string;
  sessionId: string;
  /** Asset ids this asset was derived from, as JSON (a list column would need a typed writer). */
  derivedFrom: string;
  /** `<algo>:<value>:<size>`, or "" where the object had no full-object checksum. */
  checksum: string;
  /** Cognito sub of whoever caused it (`addedBy`, or `removedBy` for a removal). */
  actor: string;
}

/** The Iceberg column order. `buildInsertSql` emits values in exactly this order. */
export const COLUMNS = [
  "event_id", "event_time", "event_type", "project_id", "asset_id", "status",
  "source", "role", "visibility", "session_id", "derived_from", "checksum", "actor",
] as const;

/** Rows per statement, and the statement-size ceiling (Athena rejects a query over 262,144 bytes). */
export const MAX_ROWS = 400;
export const MAX_SQL_BYTES = 200_000;

const ASSET_SK = "ASSET#";

/** A DynamoDB stream record, as the Lambda event delivers it (attribute-value shapes). */
export interface StreamRecord {
  eventName?: string;
  eventID?: string;
  dynamodb?: {
    Keys?: Record<string, { S?: string }>;
    NewImage?: Record<string, unknown>;
    OldImage?: Record<string, unknown>;
    ApproximateCreationDateTime?: number;
    SequenceNumber?: string;
  };
}

/** Unwrap a DynamoDB attribute value to a plain string (the only shapes an ASSET row uses here). */
function str(attr: unknown): string {
  if (!attr || typeof attr !== "object") return "";
  const a = attr as Record<string, unknown>;
  if (typeof a.S === "string") return a.S;
  if (typeof a.N === "string") return a.N;
  return "";
}

function strList(attr: unknown): string[] {
  if (!attr || typeof attr !== "object") return [];
  const a = attr as Record<string, unknown>;
  if (Array.isArray(a.L)) return (a.L as unknown[]).map(str).filter(Boolean);
  if (Array.isArray(a.SS)) return (a.SS as unknown[]).filter((x): x is string => typeof x === "string");
  return [];
}

/** `{algo, value, size}` (a DynamoDB map) → `<algo>:<value>:<size>`, or "" when absent/partial. */
function checksumOf(attr: unknown): string {
  const m = (attr as { M?: Record<string, unknown> } | undefined)?.M;
  if (!m) return "";
  const algo = str(m.algo);
  const value = str(m.value);
  const size = str(m.size);
  return algo && value ? `${algo}:${value}:${size}` : "";
}

/** `PROJECT#<pid>` → `<pid>`. */
function projectIdOf(pk: string): string {
  return pk.startsWith("PROJECT#") ? pk.slice("PROJECT#".length) : pk;
}

function sessionIdOf(image: Record<string, unknown>): string {
  const direct = str(image.sessionId);
  if (direct) return direct;
  const producer = (image.producer as { M?: Record<string, unknown> } | undefined)?.M;
  return producer ? str(producer.sessionId) : "";
}

/**
 * The event time: the row's own statusChangedAt/registeredAt where it has one, else the stream
 * record's `ApproximateCreationDateTime` (seconds). Preferring the record over the appender's clock
 * keeps a replayed or retried batch on the same timeline; `Date.now()` is only the last resort for a
 * record that carries neither. Never empty — `event_time` is a required, partitioned column, so a
 * malformed row timestamp must not take the whole batch's statement down with it.
 */
function eventTimeOf(record: StreamRecord, image: Record<string, unknown>, type: AuditEventType): string {
  const fromRow = type === "registered" ? str(image.registeredAt)
    : type === "removed" ? (str(image.removedAt) || str(image.statusChangedAt))
      : str(image.statusChangedAt);
  if (/^\d{4}-\d{2}-\d{2}T/.test(fromRow)) {
    const parsed = isoSeconds(fromRow);
    if (parsed) return parsed;
  }
  const secs = record.dynamodb?.ApproximateCreationDateTime;
  return isoSeconds(new Date(secs ? secs * 1000 : Date.now()).toISOString())
    || isoSeconds(new Date().toISOString());
}

/** Normalise to `YYYY-MM-DD HH:MM:SS.mmm` — the literal form an Iceberg timestamp cast accepts. */
export function isoSeconds(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toISOString().replace("T", " ").replace("Z", "").slice(0, 23);
}

/** One stream record → at most one audit event (`null` when it changes nothing we audit). */
export function toAuditEvent(record: StreamRecord): AuditEvent | null {
  const d = record.dynamodb;
  const sk = d?.Keys?.SK?.S ?? "";
  const pk = d?.Keys?.PK?.S ?? "";
  if (!sk.startsWith(ASSET_SK) || !pk.startsWith("PROJECT#")) return null;

  const newImage = (d?.NewImage ?? {}) as Record<string, unknown>;
  const oldImage = (d?.OldImage ?? {}) as Record<string, unknown>;
  const name = record.eventName;

  let type: AuditEventType;
  let image: Record<string, unknown>;
  if (name === "INSERT") {
    type = "registered";
    image = newImage;
  } else if (name === "REMOVE") {
    // The row itself is gone: a purge (project purge or a hard delete). Describe it from the old image.
    type = "purged";
    image = oldImage;
  } else if (name === "MODIFY") {
    const before = str(oldImage.status);
    const after = str(newImage.status);
    if (before === after) return null; // rowVersion / queue bookkeeping: not an audit event
    type = after === "removed" ? "removed" : "status";
    image = newImage;
  } else {
    return null;
  }

  const assetId = str(image.assetId) || sk.slice(ASSET_SK.length);
  const actor = type === "removed" || type === "purged"
    ? (str(image.removedBy) || str(image.addedBy))
    : str(image.addedBy);
  const derived = strList(image.derivedFrom);
  return {
    eventId: `${record.eventID ?? d?.SequenceNumber ?? ""}:${type}`,
    eventTime: eventTimeOf(record, image, type),
    eventType: type,
    projectId: projectIdOf(pk),
    assetId,
    status: str(image.status) || (type === "purged" ? str(oldImage.status) : ""),
    source: str(image.source),
    role: str(image.role),
    visibility: str(image.visibility),
    sessionId: sessionIdOf(image),
    derivedFrom: derived.length ? JSON.stringify(derived) : "",
    checksum: checksumOf(image.checksum),
    actor,
  };
}

/** The audit events of a whole stream batch, in delivery order. */
export function toAuditEvents(records: StreamRecord[]): AuditEvent[] {
  const out: AuditEvent[] = [];
  for (const r of records) {
    const e = toAuditEvent(r);
    if (e) out.push(e);
  }
  return out;
}

/** Single-quote escaping for a SQL string literal (the only user-controlled text in the statement). */
export function sqlString(v: string): string {
  return `'${String(v ?? "").replace(/'/g, "''")}'`;
}

function valuesRow(e: AuditEvent): string {
  const cells = [
    sqlString(e.eventId),
    `TIMESTAMP ${sqlString(e.eventTime)}`,
    sqlString(e.eventType),
    sqlString(e.projectId),
    sqlString(e.assetId),
    sqlString(e.status),
    sqlString(e.source),
    sqlString(e.role),
    sqlString(e.visibility),
    sqlString(e.sessionId),
    sqlString(e.derivedFrom),
    sqlString(e.checksum),
    sqlString(e.actor),
  ];
  return `(${cells.join(",")})`;
}

/**
 * ONE `INSERT INTO … VALUES …` per call = one Iceberg commit. `qualifiedTable` is the fully
 * qualified S3 Tables name (`"s3tablescatalog/<bucket>"."<namespace>"."asset_events"`).
 */
export function buildInsertSql(qualifiedTable: string, events: AuditEvent[]): string {
  if (events.length === 0) throw new Error("buildInsertSql: no events");
  return `INSERT INTO ${qualifiedTable} (${COLUMNS.join(", ")}) VALUES ${events.map(valuesRow).join(",")}`;
}

/**
 * Split a batch into as few statements as possible: normally one. A batch is split only when it
 * exceeds `MAX_ROWS` rows or would build a statement over `MAX_SQL_BYTES`.
 */
export function batchStatements(qualifiedTable: string, events: AuditEvent[]): string[] {
  const prefixBytes = Buffer.byteLength(`INSERT INTO ${qualifiedTable} (${COLUMNS.join(", ")}) VALUES `, "utf8");
  const out: string[] = [];
  let rows: AuditEvent[] = [];
  let bytes = prefixBytes;
  for (const e of events) {
    const rowBytes = Buffer.byteLength(valuesRow(e), "utf8") + 1; // + the joining comma
    if (rows.length > 0 && (rows.length + 1 > MAX_ROWS || bytes + rowBytes > MAX_SQL_BYTES)) {
      out.push(buildInsertSql(qualifiedTable, rows));
      rows = [];
      bytes = prefixBytes;
    }
    rows.push(e);
    bytes += rowBytes;
  }
  if (rows.length > 0) out.push(buildInsertSql(qualifiedTable, rows));
  return out;
}
