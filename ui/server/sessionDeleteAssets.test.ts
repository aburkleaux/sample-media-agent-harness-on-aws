/**
 * `DELETE /sessions/:id` drops that session's hidden "this chat only" Project_Assets
 * (asset-lineage D2 / R12 Phase 2).
 *
 * A chat-only upload is now a `visibility=session` asset of the turn's project instead of a loose
 * `artifacts/<sid>/input/` key, so deleting the session has to take those files with it. The call is
 * best effort: `remove_session_assets` is idempotent, and the delete the user asked for must succeed
 * even when the Asset_Service is unreachable.
 */
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

const invokeMock = vi.fn();
vi.mock("./projects/assetServiceClient", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./projects/assetServiceClient")>()),
  createAssetServiceClient: () => ({ invoke: invokeMock }),
}));

import { buildApp, __resetSessionAssetRemoverForTests } from "./app";
import { closeServers, listenLoopback } from "./testing/loopback";

const servers: http.Server[] = [];
const saved: Record<string, string | undefined> = {};
const VARS = ["AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "SESSIONS_TABLE", "LAB_DEFAULT_ACTOR_ID"];
const PID = "p" + "a".repeat(32);

beforeEach(() => {
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = "test-bucket";
  process.env.SESSIONS_TABLE = "test-sessions";
  process.env.DEV_BYPASS_AUTH = "true";
  process.env.LAB_DEFAULT_ACTOR_ID = "local-dev-user";
  sendMock.mockReset();
  invokeMock.mockReset();
  invokeMock.mockResolvedValue({ ok: true, value: { removed: [] } });
  __resetSessionAssetRemoverForTests();
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

async function listen(): Promise<string> {
  return listenLoopback(buildApp(), servers);
}

const flush = () => new Promise((r) => setTimeout(r, 20));

describe("DELETE /sessions/:id — hidden session assets", () => {
  it("asks the Asset_Service to remove the session's assets in the project it belonged to", async () => {
    sendMock.mockResolvedValue({ Attributes: { sessionId: "sess-1", activeProjectId: PID } });
    const res = await fetch(`${await listen()}/sessions/sess-1`, { method: "DELETE" });
    expect(res.status).toBe(200);
    await flush();

    expect(invokeMock).toHaveBeenCalledTimes(1);
    expect(invokeMock.mock.calls[0][0]).toMatchObject({
      projectId: PID,
      command: "remove_session_assets",
      args: { sessionId: "sess-1" },
    });
    // The delete read the row back so the project id is known.
    const del = sendMock.mock.calls.map((c) => c[0]).find((c: any) => c.__type === "Update");
    expect(del.input.ReturnValues).toBe("ALL_OLD");
  });

  it("skips the call when the deleted row names no project", async () => {
    sendMock.mockResolvedValue({ Attributes: { sessionId: "sess-1" } });
    const res = await fetch(`${await listen()}/sessions/sess-1`, { method: "DELETE" });
    expect(res.status).toBe(200);
    await flush();
    expect(invokeMock).not.toHaveBeenCalled();
  });

  it("still deletes the session when the Asset_Service call fails", async () => {
    sendMock.mockResolvedValue({ Attributes: { sessionId: "sess-1", activeProjectId: PID } });
    invokeMock.mockRejectedValue(new Error("asset service down"));
    const res = await fetch(`${await listen()}/sessions/sess-1`, { method: "DELETE" });
    expect(res.status).toBe(200);
    expect((await res.json()).deleted).toBe(true);
    await flush();
    expect(invokeMock).toHaveBeenCalledTimes(1);
  });
});
