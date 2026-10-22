// Run-grouped, paginated listing for GET /jobs.
//
// A workflow (plan run) spans MANY JobRegistry rows — a `plan|` parent, per-node
// `node|` rows, and raw gateway/SFN rows — all sharing a `runId`. The old list
// path ran ONE query with `Limit: 100` (no pagination) ordered by the index sort
// key `updatedAt`, so for an actor with 1000+ rows across 82 runs the panel saw
// only the newest ~100 ROWS (about 7 runs); the other ~75 runs were never
// fetched, and any row update reshuffled the window — "workflows disappearing".
//
// Here we page the actor-status-index fully (bounded by `maxRows`), group rows
// into display runs, and return the newest N COMPLETE runs from an opaque cursor.
// Every returned run has all its rows (so the client's `groupJobsBySession` never
// renders a partial/flickering run), and older runs page in on demand.
//
// Retention/TTL on terminal rows (separate follow-up) is what keeps the scanned
// total bounded; `maxRows` is a safety cap until then (`scanTruncated` flags it).

import { QueryCommand } from "@aws-sdk/lib-dynamodb";

export type JobRow = Record<string, any>;

const DEFAULT_MAX_ROWS = 4000;

/** Minimal shape of the DynamoDBDocumentClient we depend on (send). */
interface DocClientLike {
  send: (cmd: any) => Promise<{ Items?: JobRow[]; LastEvaluatedKey?: any }>;
}

/**
 * Page the actor-status-index newest-first and return every row for the actor,
 * up to `maxRows` (a safety cap). `truncated` is true when the cap was hit
 * before exhausting the actor's rows.
 */
export async function queryActorJobRows(
  ddb: DocClientLike,
  { table, index, actorId, maxRows = DEFAULT_MAX_ROWS }:
    { table: string; index: string; actorId: string; maxRows?: number },
): Promise<{ items: JobRow[]; truncated: boolean }> {
  const items: JobRow[] = [];
  let exclusiveStartKey: any = undefined;
  do {
    const res = await ddb.send(new QueryCommand({
      TableName: table,
      IndexName: index,
      KeyConditionExpression: "actorId = :actor",
      ExpressionAttributeValues: { ":actor": actorId },
      ScanIndexForward: false, // newest updatedAt first
      ExclusiveStartKey: exclusiveStartKey,
    }));
    for (const it of res.Items ?? []) {
      items.push(it);
      if (items.length >= maxRows) return { items, truncated: true };
    }
    exclusiveStartKey = res.LastEvaluatedKey;
  } while (exclusiveStartKey);
  return { items, truncated: false };
}

/**
 * The display-group key for a row — MUST match the client's grouping
 * (`groupJobsBySession`): a `runId` run, else a session group, else the
 * standalone job itself. Grouping server-side by the same key guarantees we
 * return complete client-render units.
 */
export function runKeyOf(item: JobRow): string {
  const runId = item?.runId ?? "";
  if (runId) return `run:${runId}`;
  const sessionId = item?.sessionId ?? "";
  if (sessionId) return `session:${sessionId}`;
  return `job:${item?.jobId ?? ""}`;
}

/**
 * Group rows into display runs, order runs by recency (newest max
 * updatedAt/startAt first), and return the rows of runs `[offset, offset+limit)`
 * — flattened, all rows of each included run. `nextOffset` is the offset of the
 * next page or null when there are no more runs.
 */
export function selectRecentRuns(
  items: JobRow[],
  { limit = 50, offset = 0 }: { limit?: number; offset?: number } = {},
): { rows: JobRow[]; totalRuns: number; nextOffset: number | null } {
  const groups = new Map<string, { rows: JobRow[]; recency: string; seq: number }>();
  let seq = 0;
  for (const it of items) {
    const key = runKeyOf(it);
    const t = String(it?.updatedAt ?? it?.startAt ?? "");
    let g = groups.get(key);
    if (!g) {
      g = { rows: [], recency: t, seq: seq++ };
      groups.set(key, g);
    }
    g.rows.push(it);
    if (t > g.recency) g.recency = t;
  }
  const ordered = [...groups.values()].sort((a, b) => {
    if (a.recency !== b.recency) return a.recency < b.recency ? 1 : -1; // recency desc
    return a.seq - b.seq; // stable: first-seen (already newest-first from the query) wins ties
  });
  const totalRuns = ordered.length;
  const start = Math.max(0, offset);
  const page = ordered.slice(start, start + Math.max(1, limit));
  const rows = page.flatMap((g) => g.rows);
  const nextOffset = start + limit < totalRuns ? start + limit : null;
  return { rows, totalRuns, nextOffset };
}

/**
 * Every row for ONE session, order-preserving. Backs GET /jobs?sessionId=… so an
 * opened session's jobs load regardless of recency (the default recent-runs
 * response is actor-wide and bounded to the newest runs, so old sessions' rows
 * fall outside it). No run-paging: a session's rows are a small subset.
 */
export function selectSessionJobs(items: JobRow[], sessionId: string): JobRow[] {
  const sid = (sessionId ?? "").trim();
  if (!sid) return [];
  return items.filter((it) => String(it?.sessionId ?? "") === sid);
}

/** Opaque cursor = base64 of the run offset. */
export function encodeJobsCursor(offset: number | null): string | null {
  if (offset === null || !Number.isFinite(offset) || offset <= 0) return null;
  return Buffer.from(String(offset), "utf8").toString("base64");
}

export function decodeJobsCursor(cursor: string | null | undefined): number {
  if (!cursor) return 0;
  try {
    const n = parseInt(Buffer.from(cursor, "base64").toString("utf8"), 10);
    return Number.isFinite(n) && n > 0 ? n : 0;
  } catch {
    return 0;
  }
}
