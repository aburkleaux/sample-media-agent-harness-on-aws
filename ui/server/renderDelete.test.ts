// E (X-brings-tab-back): the render list is derived from the S3 manifests (GET /sessions/:id/renders),
// so a closed tab must be deleted at the source or the next load re-lists it. DELETE
// /sessions/:id/renders?key=<manifest> removes the manifest, constrained to THIS session's render
// prefix so a caller can never delete arbitrary objects — and only in a session the caller OWNS
// (the same gate as POST/PATCH, see renderViews.test.ts).
import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import http from "http";

const OWNER = "local-dev-user";

const ddbSend = vi.fn();
vi.mock("@aws-sdk/lib-dynamodb", () => {
  class Cmd { input: any; __type: string; constructor(i: any, t: string) { this.input = i; this.__type = t; } }
  return {
    DynamoDBDocumentClient: { from: () => ({ send: ddbSend }) },
    GetCommand: class extends Cmd { constructor(i: any) { super(i, "Get"); } },
    DeleteCommand: class extends Cmd { constructor(i: any) { super(i, "Delete"); } },
    QueryCommand: class extends Cmd { constructor(i: any) { super(i, "Query"); } },
    PutCommand: class extends Cmd { constructor(i: any) { super(i, "Put"); } },
    UpdateCommand: class extends Cmd { constructor(i: any) { super(i, "Update"); } },
  };
});
const s3Send = vi.fn();
vi.mock("@aws-sdk/client-s3", () => {
  class Cmd { input: any; __type: string; constructor(i: any, t: string) { this.input = i; this.__type = t; } }
  return {
    S3Client: class { send = s3Send; },
    ListObjectsV2Command: class extends Cmd { constructor(i: any) { super(i, "List"); } },
    GetObjectCommand: class extends Cmd { constructor(i: any) { super(i, "GetObj"); } },
    PutObjectCommand: class extends Cmd { constructor(i: any) { super(i, "PutObj"); } },
    DeleteObjectCommand: class extends Cmd { constructor(i: any) { super(i, "DelObj"); } },
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
  // The whole /sessions route block is gated on SESSIONS_TABLE (server/app.ts: `if (ddbClient &&
  // sessionsTable)`), so a placeholder table name is needed to reach the route.
  process.env.SESSIONS_TABLE = "test-sessions";
  // Reach the gated /sessions route without a real Cognito token.
  process.env.DEV_BYPASS_AUTH = "true";
  process.env.LAB_DEFAULT_ACTOR_ID = OWNER;
  ddbSend.mockReset();
  ddbSend.mockResolvedValue({ Item: { sessionId: "sess-1", actorId: OWNER } });
  s3Send.mockReset();
  s3Send.mockResolvedValue({});
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

function listen(): Promise<string> {
  return listenLoopback(buildApp(), servers);
}

const del = (base: string, id: string, key: string) =>
  fetch(`${base}/sessions/${id}/renders?key=${encodeURIComponent(key)}`, { method: "DELETE" });
const s3Deletes = () => s3Send.mock.calls.map((c) => c[0]).filter((c: any) => c.__type === "DelObj");

describe("DELETE /sessions/:id/renders — persisted render delete", () => {
  it("400s a key belonging to a DIFFERENT session (no arbitrary deletes)", async () => {
    const r = await del(await listen(), "sess-1", "artifacts/OTHER/render/x.json");
    expect(r.status).toBe(400);
    expect(s3Deletes()).toHaveLength(0);
  });

  it("400s a traversal / non-manifest-shaped key", async () => {
    const r = await del(await listen(), "sess-1", "artifacts/sess-1/render/../../secrets.json");
    expect(r.status).toBe(400);
  });

  it("accepts a valid in-session manifest key and echoes the deleted key", async () => {
    const key = "artifacts/sess-1/render/abc123.json";
    const r = await del(await listen(), "sess-1", key);
    expect(r.status).toBe(200);
    const body = await r.json();
    expect(body.deleted).toBe(key);
    expect(s3Deletes()[0].input).toEqual({ Bucket: "test-bucket", Key: key });
  });

  it("404s (and deletes nothing) in another actor's session", async () => {
    ddbSend.mockResolvedValue({ Item: { sessionId: "sess-1", actorId: "someone-else" } });
    const r = await del(await listen(), "sess-1", "artifacts/sess-1/render/abc123.json");
    expect(r.status).toBe(404);
    expect(s3Deletes()).toHaveLength(0);
  });

  it("fails closed when the ownership lookup fails", async () => {
    ddbSend.mockRejectedValue(new Error("ddb down"));
    const r = await del(await listen(), "sess-1", "artifacts/sess-1/render/abc123.json");
    expect(r.status).toBe(404);
    expect(s3Deletes()).toHaveLength(0);
  });
});
