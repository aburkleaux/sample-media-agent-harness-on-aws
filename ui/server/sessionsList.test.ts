// Session rows can approach the 1 MB DynamoDB item ceiling (renderHistory + transcript), and the
// active-lastActivity-index projects ALL attributes. A Query returns at most 1 MB PER PAGE,
// applied BEFORE the active=1 filter — so an un-paginated GET /sessions returned only the ~1-3
// rows in the first page and the sidebar showed a near-random handful of active sessions (a
// different count on each refresh). GET /sessions must paginate over LastEvaluatedKey so EVERY
// active session is returned, and project only the lean list fields so the payload stays small.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import http from "http";

const sendMock = vi.fn();
vi.mock("@aws-sdk/lib-dynamodb", () => {
  class Cmd { input: any; __type: string; constructor(i: any, t: string) { this.input = i; this.__type = t; } }
  return {
    DynamoDBDocumentClient: { from: () => ({ send: sendMock }) },
    GetCommand: class extends Cmd { constructor(i: any) { super(i, "Get"); } },
    DeleteCommand: class extends Cmd { constructor(i: any) { super(i, "Delete"); } },
    QueryCommand: class extends Cmd { constructor(i: any) { super(i, "Query"); } },
    PutCommand: class extends Cmd { constructor(i: any) { super(i, "Put"); } },
    UpdateCommand: class extends Cmd { constructor(i: any) { super(i, "Update"); } },
  };
});

import { buildApp } from "./app";
import { closeServers, listenLoopback } from "./testing/loopback";

const servers: http.Server[] = [];
const saved: Record<string, string | undefined> = {};
const VARS = ["AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "SESSIONS_TABLE", "LAB_DEFAULT_ACTOR_ID"];

beforeEach(() => {
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = "test-bucket";
  process.env.SESSIONS_TABLE = "test-sessions";
  process.env.DEV_BYPASS_AUTH = "true";
  process.env.LAB_DEFAULT_ACTOR_ID = "local-dev-user";
  sendMock.mockReset();
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

function listen(): Promise<string> {
  return listenLoopback(buildApp(), servers);
}

describe("GET /sessions — paginates across 1 MB pages", () => {
  it("returns EVERY active session by following LastEvaluatedKey, not just the first page", async () => {
    // The GSI returns 1 MB per page — here modelled as 2 rows/page — so 3 active sessions span
    // two pages. An un-paginated handler would have returned only page 1 (2 sessions).
    sendMock.mockImplementation((cmd: any) => {
      if (cmd.__type !== "Query") return Promise.resolve({});
      if (!cmd.input.ExclusiveStartKey) {
        return Promise.resolve({
          Items: [{ sessionId: "s1", title: "one", active: 1 }, { sessionId: "s2", title: "two", active: 1 }],
          LastEvaluatedKey: { sessionId: "s2" },
        });
      }
      return Promise.resolve({ Items: [{ sessionId: "s3", title: "three", active: 1 }] });
    });

    const res = await fetch(`${await listen()}/sessions`);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.sessions.map((s: any) => s.sessionId)).toEqual(["s1", "s2", "s3"]);

    const queries = sendMock.mock.calls.map((c) => c[0]).filter((c: any) => c.__type === "Query");
    expect(queries.length).toBe(2); // followed the LastEvaluatedKey to page 2
    // Second page carried the cursor; both pages projected only the lean list fields (no
    // renderHistory/transcript), which is what keeps the payload small.
    expect(queries[1].input.ExclusiveStartKey).toEqual({ sessionId: "s2" });
    expect(queries[0].input.ProjectionExpression).toContain("sessionId");
    expect(queries[0].input.ProjectionExpression).not.toContain("renderHistory");
  });
});
