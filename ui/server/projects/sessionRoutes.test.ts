// Session-route changes for Projects (media-harness-projects-access task 6.11; the
// 6.13 checks). Mounted through the real `buildApp()` with the sessions DynamoDB client mocked (as in
// `sessionLock.test.ts`) and the Project_Registry on a real `ProjectRegistry` over `InMemoryTransactStore`.
//
// The "disabled" block is the regression guard: with no projects store, POST/PATCH /sessions must write
// exactly the rows they wrote before this feature (D7).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
    BatchGetCommand: class extends Cmd { constructor(i: any) { super(i, "BatchGet"); } },
    TransactWriteCommand: class extends Cmd { constructor(i: any) { super(i, "TransactWrite"); } },
  };
});

import { buildApp } from "../app";
import { closeServers, listenLoopback } from "../testing/loopback";
import { checkAccess, ensurePersonalProject, projectRegistry, resetProjectsStore, setProjectsStore } from "./access";
import { InMemoryTransactStore } from "./inMemoryStore";

const ACTOR = "local-dev-user";
const OTHER = "sub-other";

const servers: http.Server[] = [];
const saved: Record<string, string | undefined> = {};
const VARS = [
  "AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "SESSIONS_TABLE", "LAB_DEFAULT_ACTOR_ID",
  "PROJECTS_TABLE", "PROJECTS_STORE",
];
let store: InMemoryTransactStore;

beforeEach(() => {
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = "test-bucket";
  process.env.SESSIONS_TABLE = "test-sessions";
  process.env.DEV_BYPASS_AUTH = "true";
  process.env.LAB_DEFAULT_ACTOR_ID = ACTOR;
  delete process.env.PROJECTS_TABLE;
  delete process.env.PROJECTS_STORE;
  sendMock.mockReset();
  sendMock.mockResolvedValue({});
  store = new InMemoryTransactStore();
  setProjectsStore(store, { sleep: async () => {} });
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
  resetProjectsStore();
  vi.restoreAllMocks();
});

function listen(): Promise<string> {
  return listenLoopback(buildApp(), servers);
}

const sent = (type: string) => sendMock.mock.calls.map((c) => c[0]).filter((c: any) => c.__type === type);

async function post(base: string, body: unknown) {
  return fetch(`${base}/sessions`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}
async function patch(base: string, id: string, body: unknown) {
  return fetch(`${base}/sessions/${id}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** A non-Personal Project owned by `owner`; returns its id. */
async function ownedProject(owner: string, name: string): Promise<string> {
  const r = await projectRegistry().create(owner, name);
  if (!r.ok) throw new Error(`create failed: ${JSON.stringify(r.error)}`);
  return r.value.projectId;
}

describe("POST /sessions with Projects enabled", () => {
  it("puts the Personal_Project in the one conditional PutItem", async () => {
    const res = await post(await listen(), { sessionId: "sess-1", title: "Hello" });
    expect(res.status).toBe(201);
    const { createdAt } = await res.json();

    const puts = sent("Put");
    expect(puts).toHaveLength(1);
    const { Item, ConditionExpression } = puts[0].input;
    expect(ConditionExpression).toBe("attribute_not_exists(sessionId)");
    const personal = await ensurePersonalProject(ACTOR);
    expect(Item.activeProjectId).toBe(personal.projectId);
    // The pre-existing attributes are unchanged.
    expect(Item).toMatchObject({ sessionId: "sess-1", actorId: ACTOR, title: "Hello", active: 1, createdAt, lastActivityAt: createdAt });
  });

  it("still creates the session when the registry is unavailable", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(store, "batchGet").mockRejectedValue(new Error("ddb down"));
    vi.spyOn(store, "transactWrite").mockRejectedValue(new Error("ddb down"));
    const res = await post(await listen(), { sessionId: "sess-1" });
    expect(res.status).toBe(201);
    const { Item } = sent("Put")[0].input;
    expect(Item.activeProjectId).toBeUndefined();
  });
});

describe("PATCH /sessions/:id with Projects enabled", () => {
  it("runs checkAccess, then sets activeProjectId, activeProjectSetAt and webSearch", async () => {
    const pid = await ownedProject(ACTOR, "Alpha");
    const res = await patch(await listen(), "sess-1", { activeProjectId: pid, webSearch: true });
    expect(res.status).toBe(200);
    const { lastActivityAt } = await res.json();
    const upd = sent("Update");
    expect(upd).toHaveLength(1);
    const { UpdateExpression, ExpressionAttributeValues, ConditionExpression } = upd[0].input;
    expect(UpdateExpression).toContain("activeProjectId = :activeProjectId");
    expect(UpdateExpression).toContain("activeProjectSetAt = :activeProjectSetAt");
    expect(UpdateExpression).toContain("webSearch = :webSearch");
    expect(ExpressionAttributeValues[":activeProjectId"]).toBe(pid);
    expect(ExpressionAttributeValues[":activeProjectSetAt"]).toBe(Date.parse(lastActivityAt));
    expect(ExpressionAttributeValues[":webSearch"]).toBe(true);
    // The lock guard still applies.
    expect(ConditionExpression).toContain("#locked");
  });

  it("answers the uniform 404 and writes nothing for a Project the Actor cannot use", async () => {
    const foreign = await ownedProject(OTHER, "Theirs");
    const base = await listen();
    const bodies: string[] = [];
    for (const id of [foreign, "p0123456789abcdef0123456789abcdef", "not-an-id", 42]) {
      const res = await patch(base, "sess-1", { activeProjectId: id, title: "renamed" });
      expect(res.status, String(id)).toBe(404);
      bodies.push(await res.text());
    }
    expect(new Set(bodies)).toEqual(new Set([JSON.stringify({ error: { code: "project_not_found", message: "Project not found" } })]));
    expect(sent("Update")).toHaveLength(0);
    // Sanity: the denial is checkAccess's, not a route-local rule.
    expect((await checkAccess(ACTOR, foreign)).ok).toBe(false);
  });

  it("rejects a non-boolean webSearch with 400 and no write", async () => {
    const res = await patch(await listen(), "sess-1", { webSearch: "yes" });
    expect(res.status).toBe(400);
    expect(sent("Update")).toHaveLength(0);
  });

  it("leaves a plain activity PATCH unchanged", async () => {
    const res = await patch(await listen(), "sess-1", { title: "t" });
    expect(res.status).toBe(200);
    expect(sent("Update")[0].input.UpdateExpression).toBe("SET lastActivityAt = :now, title = :title");
  });

  describe("session ↔ project binding (D1, R2)", () => {
    const TURN = [{ id: "t-0-u", role: "user", content: "earlier", timestamp: 1 }];
    const row = (extra: Record<string, unknown>) => {
      sendMock.mockImplementation(async (cmd: any) =>
        cmd.__type === "Get" ? { Item: { sessionId: "sess-1", actorId: ACTOR, ...extra } } : {});
    };

    it("answers 409 session_project_fixed and writes nothing for another project once the session has a turn", async () => {
      const bound = await ownedProject(ACTOR, "Alpha");
      const other = await ownedProject(ACTOR, "Beta");
      row({ activeProjectId: bound, transcript: TURN });
      const res = await patch(await listen(), "sess-1", { activeProjectId: other, webSearch: true });
      expect(res.status).toBe(409);
      expect((await res.json()).error).toMatchObject({ code: "session_project_fixed", projectId: bound });
      expect(sent("Update")).toHaveLength(0);
      const get = sent("Get")[0].input;
      expect(get).toMatchObject({ Key: { sessionId: "sess-1" }, ConsistentRead: true });
    });

    it("allows the bound project again, and any project while the session has no turn", async () => {
      const bound = await ownedProject(ACTOR, "Alpha");
      const other = await ownedProject(ACTOR, "Beta");
      const base = await listen();
      row({ activeProjectId: bound, transcript: TURN });
      expect((await patch(base, "sess-1", { activeProjectId: bound })).status).toBe(200);
      row({ activeProjectId: bound, transcript: [] });
      expect((await patch(base, "sess-1", { activeProjectId: other })).status).toBe(200);
      expect(sent("Update").at(-1).input.ExpressionAttributeValues[":activeProjectId"]).toBe(other);
    });

    it("never refuses for another actor's row, and fails open when the row cannot be read", async () => {
      const other = await ownedProject(ACTOR, "Beta");
      const base = await listen();
      sendMock.mockImplementation(async (cmd: any) =>
        cmd.__type === "Get" ? { Item: { sessionId: "sess-1", actorId: OTHER, activeProjectId: "px", transcript: TURN } } : {});
      // The update's ownership condition (not this check) answers for a foreign row.
      expect((await patch(base, "sess-1", { activeProjectId: other })).status).toBe(200);
      vi.spyOn(console, "warn").mockImplementation(() => {});
      sendMock.mockImplementation(async (cmd: any) => {
        if (cmd.__type === "Get") throw new Error("ddb down");
        return {};
      });
      expect((await patch(base, "sess-1", { activeProjectId: other })).status).toBe(200);
    });
  });
});

describe("GET /config projectsEnabled", () => {
  it("is true with a projects store", async () => {
    const res = await fetch(`${await listen()}/config?t=1`); // the query dodges vitest.setup.ts's `/config` fetch stub
    expect((await res.json()).projectsEnabled).toBe(true);
  });
  it("is false without one", async () => {
    setProjectsStore(null);
    const res = await fetch(`${await listen()}/config?t=1`); // the query dodges vitest.setup.ts's `/config` fetch stub
    expect((await res.json()).projectsEnabled).toBe(false);
  });
});

describe("session routes with Projects disabled (unchanged behaviour)", () => {
  beforeEach(() => setProjectsStore(null));

  it("POST /sessions writes exactly the pre-Projects row", async () => {
    const res = await post(await listen(), { sessionId: "sess-1", title: "Hello" });
    expect(res.status).toBe(201);
    const body = await res.json();
    expect(Object.keys(body).sort()).toEqual(["createdAt", "sessionId"]);
    const puts = sent("Put");
    expect(puts).toHaveLength(1);
    expect(puts[0].input).toEqual({
      TableName: "test-sessions",
      Item: {
        sessionId: "sess-1",
        actorId: ACTOR,
        title: "Hello",
        active: 1,
        createdAt: body.createdAt,
        lastActivityAt: body.createdAt,
      },
      ConditionExpression: "attribute_not_exists(sessionId)",
    });
    // No registry traffic of any kind.
    expect(sent("BatchGet")).toHaveLength(0);
    expect(sent("TransactWrite")).toHaveLength(0);
  });

  it("PATCH /sessions/:id ignores activeProjectId and webSearch", async () => {
    const res = await patch(await listen(), "sess-1", {
      activeProjectId: "not-an-id",
      webSearch: "anything",
    });
    expect(res.status).toBe(200);
    expect(Object.keys(await res.json()).sort()).toEqual(["lastActivityAt", "sessionId"]);
    const upd = sent("Update");
    expect(upd).toHaveLength(1);
    expect(upd[0].input.UpdateExpression).toBe("SET lastActivityAt = :now");
    // Only the activity timestamp plus the ownership/live/lock guard values; no Projects key is written.
    expect(Object.keys(upd[0].input.ExpressionAttributeValues).sort()).toEqual([":actor", ":now", ":one", ":unlocked"]);
  });
});
