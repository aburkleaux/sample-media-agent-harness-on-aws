// Full-freeze session lock enforcement (server side). A locked session must
// reject delete, rename, any renderHistory/activity write, and new turns
// (POST /api/invocations) with 423; only the lock toggle itself (PATCH
// { locked }) is permitted, so a frozen session can always be unlocked.
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
  sendMock.mockResolvedValue({});
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

function listen(): Promise<string> {
  return listenLoopback(buildApp(), servers);
}

/**
 * Reject Update commands as a conditional failure would. `item` is the `ReturnValuesOnConditionCheckFailure:
 * ALL_OLD` image DynamoDB attaches (marshalled), or none for a row that does not exist.
 */
function conditionalFail(item?: Record<string, unknown>) {
  const e: any = new Error("The conditional request failed");
  e.name = "ConditionalCheckFailedException";
  if (item) e.Item = item;
  return Promise.reject(e);
}
/** The caller's own row, locked. */
const lockedUpdate = (cmd: any) =>
  cmd.__type === "Update"
    ? conditionalFail({ sessionId: { S: "sess-1" }, actorId: { S: "local-dev-user" }, locked: { BOOL: true } })
    : Promise.resolve({});
const updates = () => sendMock.mock.calls.map((c) => c[0]).filter((c: any) => c.__type === "Update");

describe("DELETE /sessions/:id — lock enforcement", () => {
  it("returns 423 when the row is locked (conditional check fails)", async () => {
    sendMock.mockImplementation(lockedUpdate);
    const res = await fetch(`${await listen()}/sessions/sess-1`, { method: "DELETE" });
    expect(res.status).toBe(423);
    expect((await res.json()).locked).toBe(true);
    // The delete carried the guard so an unlocked-only delete is possible.
    const del = updates()[0];
    expect(del.input.ConditionExpression).toContain("#locked");
    expect(del.input.ExpressionAttributeNames["#locked"]).toBe("locked");
  });

  it("soft-deletes (200) when the row is unlocked", async () => {
    sendMock.mockResolvedValue({});
    const res = await fetch(`${await listen()}/sessions/sess-1`, { method: "DELETE" });
    expect(res.status).toBe(200);
    expect((await res.json()).deleted).toBe(true);
    // Only the caller's existing row: an unknown id must not create an orphan row.
    const del = updates()[0];
    expect(del.input.ConditionExpression).toContain("attribute_exists(sessionId)");
    expect(del.input.ExpressionAttributeValues[":actor"]).toBe("local-dev-user");
  });

  it("returns 404 for a missing or another actor's session, creating nothing", async () => {
    sendMock.mockImplementation((cmd: any) => (cmd.__type === "Update" ? conditionalFail() : Promise.resolve({})));
    expect((await fetch(`${await listen()}/sessions/nope`, { method: "DELETE" })).status).toBe(404);
    sendMock.mockImplementation((cmd: any) => (cmd.__type === "Update"
      ? conditionalFail({ sessionId: { S: "sess-1" }, actorId: { S: "someone-else" }, locked: { BOOL: true } })
      : Promise.resolve({})));
    expect((await fetch(`${await listen()}/sessions/sess-1`, { method: "DELETE" })).status).toBe(404);
  });
});

describe("PATCH /sessions/:id — a deleted session takes no writes", () => {
  it("conditions every PATCH on the row being live, so a deleted row answers 404", async () => {
    sendMock.mockImplementation((cmd: any) => (cmd.__type === "Update"
      ? conditionalFail({ sessionId: { S: "sess-1" }, actorId: { S: "local-dev-user" }, active: { N: "0" } })
      : Promise.resolve({})));
    const res = await fetch(`${await listen()}/sessions/sess-1`, {
      method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ title: "x" }),
    });
    expect(res.status).toBe(404);
    const patch = updates()[0];
    expect(patch.input.ConditionExpression).toContain("active = :one");
    expect(patch.input.ExpressionAttributeValues[":one"]).toBe(1);
  });
});

describe("PATCH /sessions/:id — lock enforcement", () => {
  it("returns 423 on rename when locked", async () => {
    sendMock.mockImplementation(lockedUpdate);
    const res = await fetch(`${await listen()}/sessions/sess-1`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ title: "renamed" }),
    });
    expect(res.status).toBe(423);
    expect((await res.json()).locked).toBe(true);
  });

  it("returns 423 on a renderHistory/activity write when locked", async () => {
    sendMock.mockImplementation(lockedUpdate);
    const res = await fetch(`${await listen()}/sessions/sess-1`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ renderHistory: [] }),
    });
    expect(res.status).toBe(423);
  });

  it("allows the lock toggle (200) — SET #locked, ownership-guarded only (no lock guard)", async () => {
    sendMock.mockResolvedValue({});
    const res = await fetch(`${await listen()}/sessions/sess-1`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ locked: true }),
    });
    expect(res.status).toBe(200);
    expect((await res.json()).locked).toBe(true);
    const upd = updates()[0];
    expect(upd.input.UpdateExpression).toBe("SET #locked = :locked");
    expect(upd.input.ExpressionAttributeValues[":locked"]).toBe(true);
    // Toggle must NOT carry the lock guard, else a locked session could never be unlocked. It IS
    // guarded by ownership, so it can neither create an orphan row nor lock another actor's session.
    expect(upd.input.ConditionExpression).toBe("attribute_exists(sessionId) AND #actor = :actor");
    expect(upd.input.ConditionExpression).not.toContain(":unlocked");
    expect(upd.input.ExpressionAttributeValues[":actor"]).toBe("local-dev-user");
  });

  it("allows unlock (locked:false) even when currently locked", async () => {
    sendMock.mockResolvedValue({});
    const res = await fetch(`${await listen()}/sessions/sess-1`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ locked: false }),
    });
    expect(res.status).toBe(200);
    expect((await res.json()).locked).toBe(false);
    expect(updates()[0].input.ExpressionAttributeValues[":locked"]).toBe(false);
  });
});

describe("PATCH /sessions/:id — never creates a row (ownership condition)", () => {
  const patchBody = (body: unknown) => ({
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

  it("conditions every write on the row existing and belonging to the caller", async () => {
    sendMock.mockResolvedValue({});
    const res = await fetch(`${await listen()}/sessions/sess-1`, patchBody({ title: "t" }));
    expect(res.status).toBe(200);
    const upd = updates()[0];
    expect(upd.input.ConditionExpression).toContain("attribute_exists(sessionId) AND #actor = :actor");
    expect(upd.input.ExpressionAttributeNames["#actor"]).toBe("actorId");
    expect(upd.input.ExpressionAttributeValues[":actor"]).toBe("local-dev-user");
    expect(upd.input.ReturnValuesOnConditionCheckFailure).toBe("ALL_OLD");
  });

  it("is 404 JSON for an unknown session id (no row is created)", async () => {
    sendMock.mockImplementation((cmd: any) => (cmd.__type === "Update" ? conditionalFail() : Promise.resolve({})));
    const res = await fetch(`${await listen()}/sessions/ghost`, patchBody({ renderHistory: [] }));
    expect(res.status).toBe(404);
    expect(res.headers.get("content-type")).toContain("application/json");
    expect(await res.json()).toMatchObject({ code: "session_not_found", sessionId: "ghost" });
  });

  it("is 404 (not 423) for another actor's session, even a locked one", async () => {
    sendMock.mockImplementation((cmd: any) =>
      cmd.__type === "Update"
        ? conditionalFail({ actorId: { S: "someone-else" }, locked: { BOOL: true } })
        : Promise.resolve({}));
    const res = await fetch(`${await listen()}/sessions/sess-x`, patchBody({ title: "t" }));
    expect(res.status).toBe(404);
  });

  it("is 404 for a lock toggle on a missing or foreign session", async () => {
    sendMock.mockImplementation((cmd: any) => (cmd.__type === "Update" ? conditionalFail() : Promise.resolve({})));
    const res = await fetch(`${await listen()}/sessions/ghost`, patchBody({ locked: true }));
    expect(res.status).toBe(404);
    expect((await res.json()).code).toBe("session_not_found");
  });

  it("still answers 423 when the caller's own row is locked (unmarshalled old image too)", async () => {
    sendMock.mockImplementation((cmd: any) =>
      cmd.__type === "Update" ? conditionalFail({ actorId: "local-dev-user", locked: true }) : Promise.resolve({}));
    const res = await fetch(`${await listen()}/sessions/sess-1`, patchBody({ title: "t" }));
    expect(res.status).toBe(423);
  });
});

describe("POST /api/invocations — lock gate on new turns", () => {
  it("returns 423 (no runtime invoke) when the session is locked", async () => {
    // Get(session) reports locked -> the gate 423s before any signed request.
    sendMock.mockImplementation((cmd: any) =>
      cmd.__type === "Get" ? Promise.resolve({ Item: { actorId: "local-dev-user", locked: true } }) : Promise.resolve({}));
    const res = await fetch(`${await listen()}/api/invocations`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: "Bearer test" },
      body: JSON.stringify({ prompt: "hello", session_id: "sess-1" }),
    });
    expect(res.status).toBe(423);
    expect((await res.json()).locked).toBe(true);
    // The gate read the session's lock state via a projected Get.
    const get = sendMock.mock.calls.map((c) => c[0]).find((c: any) => c.__type === "Get");
    expect(get.input.Key.sessionId).toBe("sess-1");
    expect(get.input.ExpressionAttributeNames["#locked"]).toBe("locked");
  });
});

describe("DELETE /sessions/:id/renders — lock enforcement (render-tab remove)", () => {
  const KEY = "artifacts/sess-1/render/tab.json";
  it("returns 423 (before any S3 delete) when the session is locked", async () => {
    sendMock.mockImplementation((cmd: any) =>
      cmd.__type === "Get" ? Promise.resolve({ Item: { actorId: "local-dev-user", locked: true } }) : Promise.resolve({}));
    const res = await fetch(`${await listen()}/sessions/sess-1/renders?key=${encodeURIComponent(KEY)}`, { method: "DELETE" });
    expect(res.status).toBe(423);
    expect((await res.json()).locked).toBe(true);
    const get = sendMock.mock.calls.map((c) => c[0]).find((c: any) => c.__type === "Get");
    expect(get.input.Key.sessionId).toBe("sess-1");
    expect(get.input.ExpressionAttributeNames["#locked"]).toBe("locked");
  });

  it("deletes (200) when the session is unlocked", async () => {
    process.env.UPLOAD_BUCKET = "";  // skip the real S3 delete (S3 client is not mocked in this suite)
    sendMock.mockImplementation((cmd: any) =>
      cmd.__type === "Get" ? Promise.resolve({ Item: { actorId: "local-dev-user", locked: false } }) : Promise.resolve({}));
    const res = await fetch(`${await listen()}/sessions/sess-1/renders?key=${encodeURIComponent(KEY)}`, { method: "DELETE" });
    expect(res.status).toBe(200);
    expect((await res.json()).deleted).toBe(KEY);
  });
});
