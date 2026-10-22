// Projects-session OUTPUT keys through the proxy's artifact routes (asset-lineage Phase 3 (a), design §9.1).
//
// `projects/<pid>/sessions/<sid>/output/<file>` is the new session output location. Pinned end to end
// through `buildApp`:
//   * `GET /api/artifact` serves it from OUR bucket, ALWAYS authenticated (even ungated);
//   * it is readable by the session owner OR a project member, and nobody else (uniform 400);
//   * a project ASSET key and a non-output session key are never served;
//   * `POST /api/artifact-urls` presigns it for a member and refuses a non-member.
// validateJwt, DynamoDB and S3 are mocked: deterministic and offline.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import http from "http";

const h = vi.hoisted(() => ({
  ddbSend: vi.fn(),
  s3Send: vi.fn(),
}));

// A token's value IS its subject; "bad" fails validation.
vi.mock("../auth/validateJwt", () => ({
  validateJwt: async (token: string) => {
    if (token === "bad") throw new Error("invalid signature");
    return { sub: token, groups: [] };
  },
}));

vi.mock("@aws-sdk/client-dynamodb", () => ({ DynamoDBClient: class {} }));

vi.mock("@aws-sdk/lib-dynamodb", () => {
  class Cmd { input: any; __type: string; constructor(i: any, t: string) { this.input = i; this.__type = t; } }
  return {
    DynamoDBDocumentClient: { from: () => ({ send: h.ddbSend }) },
    GetCommand: class extends Cmd { constructor(i: any) { super(i, "Get"); } },
    DeleteCommand: class extends Cmd { constructor(i: any) { super(i, "Delete"); } },
    QueryCommand: class extends Cmd { constructor(i: any) { super(i, "Query"); } },
    PutCommand: class extends Cmd { constructor(i: any) { super(i, "Put"); } },
    UpdateCommand: class extends Cmd { constructor(i: any) { super(i, "Update"); } },
    BatchGetCommand: class extends Cmd { constructor(i: any) { super(i, "BatchGet"); } },
    TransactWriteCommand: class extends Cmd { constructor(i: any) { super(i, "TransactWrite"); } },
  };
});

vi.mock("@aws-sdk/client-s3", () => {
  class Cmd { input: any; __type: string; constructor(i: any, t: string) { this.input = i; this.__type = t; } }
  return {
    S3Client: class { send = h.s3Send; },
    ListObjectsV2Command: class extends Cmd { constructor(i: any) { super(i, "List"); } },
    GetObjectCommand: class extends Cmd { constructor(i: any) { super(i, "GetObj"); } },
    PutObjectCommand: class extends Cmd { constructor(i: any) { super(i, "PutObj"); } },
    DeleteObjectCommand: class extends Cmd { constructor(i: any) { super(i, "DelObj"); } },
  };
});

vi.mock("@aws-sdk/s3-request-presigner", () => ({
  getSignedUrl: async (_c: any, cmd: any) =>
    `https://s3.example/${cmd.input.Bucket}/${cmd.input.Key}?X-Amz-Signature=sig`,
}));

import { buildApp } from "./app";
import { resetProjectsStore } from "./projects/access";
import { closeServers, listenLoopback } from "./testing/loopback";

const MEMBER = "actor-1";       // member of PID, does not own the session
const SESSION_OWNER = "owner-2"; // owns SID, is NOT a member of PID (e.g. removed later)
const INTRUDER = "intruder";
const BODY = "session-output-bytes";
const PID = "p" + "1".repeat(32);
const SID = "4f9d0d02-1111-4222-8333-444455556666";
const KEY = `projects/${PID}/sessions/${SID}/output/clip.mp4`;
const TABLE = "test-projects";

const servers: http.Server[] = [];
const saved: Record<string, string | undefined> = {};
const VARS = [
  "AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "USER_POOL_ISSUER", "USER_POOL_CLIENT_ID",
  "SESSIONS_TABLE", "ARTIFACTS_REQUIRE_AUTH", "MEDIA_PROXY_BUCKET", "PROJECTS_TABLE",
];

function projectsTable() {
  const items = new Map<string, any>();
  const put = (PK: string, SK: string, rest: any = {}) => items.set(`${PK}|${SK}`, { PK, SK, ...rest });
  put(`PROJECT#${PID}`, "META", {
    projectId: PID, name: "Shoot", nameFold: "shoot", ownerSub: "owner-sub",
    personal: false, status: "active", memberCount: 1, createdAt: "2026-01-01T00:00:00Z",
  });
  put(`PROJECT#${PID}`, `MEMBER#${MEMBER}`, { role: "member", display: "a@b", addedAt: "x", addedBy: "y" });
  return items;
}

function ddb(items: Map<string, any>, opts: { sessionLookupFails?: boolean } = {}) {
  return async (cmd: any) => {
    if (cmd.__type === "BatchGet") {
      const keys = cmd.input.RequestItems[TABLE].Keys as Array<{ PK: string; SK: string }>;
      return { Responses: { [TABLE]: keys.map((k) => items.get(`${k.PK}|${k.SK}`)).filter(Boolean) } };
    }
    if (cmd.__type === "Get" && cmd.input.TableName === "test-sessions") {
      if (opts.sessionLookupFails) throw new Error("throttled");
      return cmd.input.Key.sessionId === SID ? { Item: { sessionId: SID, actorId: SESSION_OWNER } } : {};
    }
    return {};
  };
}

beforeEach(() => {
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = "test-bucket";
  process.env.SESSIONS_TABLE = "test-sessions";
  process.env.DEV_BYPASS_AUTH = "false";
  process.env.USER_POOL_ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_test";
  process.env.USER_POOL_CLIENT_ID = "testclient";
  process.env.PROJECTS_TABLE = TABLE;
  delete process.env.MEDIA_PROXY_BUCKET;
  delete process.env.ARTIFACTS_REQUIRE_AUTH;
  resetProjectsStore();
  h.ddbSend.mockReset();
  h.ddbSend.mockImplementation(ddb(projectsTable()));
  h.s3Send.mockReset();
  h.s3Send.mockImplementation(async (cmd: any) => {
    if (cmd.__type === "GetObj") {
      return {
        ContentType: "video/mp4",
        ContentLength: BODY.length,
        Body: { transformToByteArray: async () => new TextEncoder().encode(BODY) },
      };
    }
    return {};
  });
});

afterEach(async () => {
  await closeServers(servers);
  resetProjectsStore();
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

const listen = () => listenLoopback(buildApp(), servers);
const art = (base: string, key: string, init?: RequestInit) =>
  fetch(`${base}/api/artifact?key=${encodeURIComponent(key)}`, init);
const bearer = (t: string) => ({ headers: { authorization: `Bearer ${t}` } });
const urls = (base: string, keys: string[], token: string) =>
  fetch(`${base}/api/artifact-urls`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify({ keys }),
  });
const s3Keys = () =>
  h.s3Send.mock.calls.filter((c) => c[0].__type === "GetObj").map((c) => `${c[0].input.Bucket}/${c[0].input.Key}`);

describe("GET /api/artifact — projects-session outputs (asset-lineage Phase 3 (a))", () => {
  it("is NEVER public: ungated mode still requires a token (401, no S3)", async () => {
    const r = await art(await listen(), KEY);
    expect(r.status).toBe(401);
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("serves a project member from OUR bucket", async () => {
    const r = await art(await listen(), KEY, bearer(MEMBER));
    expect(r.status).toBe(200);
    expect(await r.text()).toBe(BODY);
    expect(s3Keys()).toEqual([`test-bucket/${KEY}`]);
  });

  it("serves the session owner even when not a member", async () => {
    const r = await art(await listen(), KEY, bearer(SESSION_OWNER));
    expect(r.status).toBe(200);
    await r.text();
  });

  it("refuses anybody else with the uniform 400 (no S3)", async () => {
    const r = await art(await listen(), KEY, bearer(INTRUDER));
    expect(r.status).toBe(400);
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("fails closed when the session lookup errors and the caller is not a member", async () => {
    h.ddbSend.mockImplementation(ddb(projectsTable(), { sessionLookupFails: true }));
    expect((await art(await listen(), KEY, bearer(INTRUDER))).status).toBe(400);
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("never serves a project ASSET key or a non-output session key", async () => {
    const base = await listen();
    for (const k of [`projects/${PID}/video:0b6c1b5e-3a3f-4b44-9c55-111122223333/a.mp4`,
      `projects/${PID}/sessions/${SID}/render/m.json`]) {
      expect((await art(base, k, bearer(MEMBER))).status).toBe(400);
    }
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("keeps legacy artifact keys public in ungated mode, unchanged", async () => {
    const r = await art(await listen(), `artifacts/${SID}/output/a.mp4`);
    expect(r.status).toBe(200);
    await r.text();
  });
});

describe("POST /api/artifact-urls — projects-session outputs", () => {
  it("member gets a URL, non-member the uniform not_found", async () => {
    const base = await listen();
    const ok = await (await urls(base, [KEY], MEMBER)).json();
    expect(ok.urls[KEY].url).toContain(`test-bucket/${KEY}`);
    const no = await (await urls(base, [KEY], INTRUDER)).json();
    expect(no.urls[KEY]).toBeUndefined();
    expect(no.errors[KEY]).toEqual({ code: "not_found" });
  });
});
