import { describe, it, expect } from "vitest";
import {
  queryActorJobRows,
  selectRecentRuns,
  selectSessionJobs,
  runKeyOf,
  encodeJobsCursor,
  decodeJobsCursor,
  type JobRow,
} from "./jobsList";

// A fake DynamoDBDocumentClient that returns scripted pages by call order,
// ignoring the command (queryActorJobRows only cares about Items/LastEvaluatedKey).
function fakeDdb(pages: Array<{ Items?: JobRow[]; LastEvaluatedKey?: any }>) {
  let i = 0;
  return { send: async () => pages[i++] ?? { Items: [] } };
}

const row = (jobId: string, over: Partial<JobRow> = {}): JobRow => ({
  jobId,
  updatedAt: "2026-01-01T00:00:00Z",
  ...over,
});

describe("queryActorJobRows — full pagination", () => {
  it("follows LastEvaluatedKey across pages and returns every row", async () => {
    const ddb = fakeDdb([
      { Items: [row("a"), row("b")], LastEvaluatedKey: { k: 1 } },
      { Items: [row("c")], LastEvaluatedKey: { k: 2 } },
      { Items: [row("d")] }, // no LEK -> stop
    ]);
    const { items, truncated } = await queryActorJobRows(ddb, {
      table: "t", index: "i", actorId: "act",
    });
    expect(items.map((r) => r.jobId)).toEqual(["a", "b", "c", "d"]);
    expect(truncated).toBe(false);
  });

  it("stops at maxRows and flags truncated (never silently drops the rest)", async () => {
    const ddb = fakeDdb([{ Items: [row("a"), row("b"), row("c")], LastEvaluatedKey: { k: 1 } }]);
    const { items, truncated } = await queryActorJobRows(ddb, {
      table: "t", index: "i", actorId: "act", maxRows: 2,
    });
    expect(items.map((r) => r.jobId)).toEqual(["a", "b"]);
    expect(truncated).toBe(true);
  });

  it("single page with no LastEvaluatedKey is not truncated", async () => {
    const ddb = fakeDdb([{ Items: [row("a")] }]);
    const { items, truncated } = await queryActorJobRows(ddb, { table: "t", index: "i", actorId: "act" });
    expect(items.map((r) => r.jobId)).toEqual(["a"]);
    expect(truncated).toBe(false);
  });
});

describe("runKeyOf — grouping key matches the client", () => {
  it("keys by runId, else session, else the standalone job", () => {
    expect(runKeyOf(row("j", { runId: "R1" }))).toBe("run:R1");
    expect(runKeyOf(row("j", { sessionId: "S1" }))).toBe("session:S1");
    expect(runKeyOf(row("j"))).toBe("job:j");
  });
});

describe("selectRecentRuns — complete runs, newest first, paged", () => {
  it("groups by run and returns ALL rows of each returned run (no partial runs)", () => {
    const items = [
      row("A-root", { runId: "A", updatedAt: "2026-01-01T00:05:00Z" }),
      row("A-node1", { runId: "A", updatedAt: "2026-01-01T00:02:00Z" }),
      row("A-node2", { runId: "A", updatedAt: "2026-01-01T00:03:00Z" }),
    ];
    const { rows, totalRuns, nextOffset } = selectRecentRuns(items, { limit: 50 });
    expect(totalRuns).toBe(1);
    expect(rows.map((r) => r.jobId).sort()).toEqual(["A-node1", "A-node2", "A-root"]);
    expect(nextOffset).toBeNull();
  });

  it("orders runs by recency (max updatedAt) descending", () => {
    const items = [
      row("old", { runId: "OLD", updatedAt: "2026-01-01T00:00:00Z" }),
      row("new", { runId: "NEW", updatedAt: "2026-01-02T00:00:00Z" }),
      row("mid", { runId: "MID", updatedAt: "2026-01-01T12:00:00Z" }),
    ];
    const { rows } = selectRecentRuns(items, { limit: 50 });
    expect(rows.map((r) => r.runId)).toEqual(["NEW", "MID", "OLD"]);
  });

  it("pages by RUN with a stable cursor, never dropping a run", () => {
    const items = [
      row("r1", { runId: "R1", updatedAt: "2026-01-03T00:00:00Z" }),
      row("r2", { runId: "R2", updatedAt: "2026-01-02T00:00:00Z" }),
      row("r3", { runId: "R3", updatedAt: "2026-01-01T00:00:00Z" }),
    ];
    const p1 = selectRecentRuns(items, { limit: 2, offset: 0 });
    expect(p1.rows.map((r) => r.runId)).toEqual(["R1", "R2"]);
    expect(p1.totalRuns).toBe(3);
    expect(p1.nextOffset).toBe(2);
    const p2 = selectRecentRuns(items, { limit: 2, offset: p1.nextOffset! });
    expect(p2.rows.map((r) => r.runId)).toEqual(["R3"]);
    expect(p2.nextOffset).toBeNull();
  });

  it("groups no-runId rows by session, and session-less rows stand alone", () => {
    const items = [
      row("s1a", { sessionId: "S1", updatedAt: "2026-01-01T00:02:00Z" }),
      row("s1b", { sessionId: "S1", updatedAt: "2026-01-01T00:01:00Z" }),
      row("lone", {}),
    ];
    const { rows, totalRuns } = selectRecentRuns(items, { limit: 50 });
    expect(totalRuns).toBe(2); // session:S1 (2 rows) + job:lone (1 row)
    expect(rows.map((r) => r.jobId).sort()).toEqual(["lone", "s1a", "s1b"]);
  });
});

describe("jobs cursor encode/decode", () => {
  it("round-trips a positive offset and null-guards the rest", () => {
    expect(encodeJobsCursor(0)).toBeNull();
    expect(encodeJobsCursor(null)).toBeNull();
    const c = encodeJobsCursor(5)!;
    expect(typeof c).toBe("string");
    expect(decodeJobsCursor(c)).toBe(5);
    expect(decodeJobsCursor(null)).toBe(0);
    expect(decodeJobsCursor("not-base64-@@")).toBe(0);
  });
});


describe("selectSessionJobs — GET /jobs?sessionId=", () => {
  it("returns only the given session's rows, order-preserving", () => {
    const items = [
      row("a", { sessionId: "s1" }),
      row("b", { sessionId: "s2" }),
      row("c", { sessionId: "s1" }),
    ];
    expect(selectSessionJobs(items, "s1").map((r) => r.jobId)).toEqual(["a", "c"]);
  });

  it("trims the session id and never leaks all rows on a blank id", () => {
    const items = [row("a", { sessionId: "s1" }), row("b", { sessionId: "s2" })];
    expect(selectSessionJobs(items, "")).toEqual([]);
    expect(selectSessionJobs(items, "   ")).toEqual([]);
    expect(selectSessionJobs(items, " s1 ").map((r) => r.jobId)).toEqual(["a"]);
  });

  it("returns [] when no row matches the session", () => {
    expect(selectSessionJobs([row("a", { sessionId: "s1" })], "nope")).toEqual([]);
  });
});
