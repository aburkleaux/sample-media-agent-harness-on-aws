// M2C_REFERENCES through the proxy's artifact routes (asset-lineage D4/R4, Phase 2).
//
// `artifacts/m2c/<bucket>/<key>` names an object M2C wrote, referenced where it wrote it and never
// copied into the artifacts bucket. These tests pin the ROUTE wiring end to end through `buildApp`:
//   * `GET /api/artifact?key=` reads the M2C proxy bucket under the key the reference names, and is
//     ALWAYS authenticated — even in ungated mode, where every other artifact key is public;
//   * `POST /api/artifact-urls` presigns it in that bucket;
//   * authorization is project membership: a member of the project whose `SRC#` guard registers the
//     location gets a URL, a non-member and an unregistered location get the uniform refusal;
//   * with no projects store (local dev) every reference is refused and S3 is never touched.
//
// validateJwt, DynamoDB and S3 are mocked: deterministic and offline.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import http from "http";
import { createHash } from "node:crypto";

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

const OWNER = "actor-1";
const BODY = "m2c-bytes";
const M2C_BUCKET = "m2cv6-proxy-bucket";
const M2C_KEY = "micro-workflow/media-processing/EXEC/proxy/transcoded.mp4";
const REF = `artifacts/m2c/${M2C_BUCKET}/${M2C_KEY}`;
const PID = "p" + "1".repeat(32);
const TABLE = "test-projects";

const servers: http.Server[] = [];
const saved: Record<string, string | undefined> = {};
const VARS = [
  "AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "USER_POOL_ISSUER", "USER_POOL_CLIENT_ID",
  "SESSIONS_TABLE", "ARTIFACTS_REQUIRE_AUTH", "MEDIA_PROXY_BUCKET", "PROJECTS_TABLE",
];

/** The projects table, as the real key builders would write it. */
function projectsTable(opts: { registered?: boolean } = {}) {
  const items = new Map<string, any>();
  const put = (PK: string, SK: string, rest: any = {}) => items.set(`${PK}|${SK}`, { PK, SK, ...rest });
  put(`PROJECT#${PID}`, "META", {
    projectId: PID, name: "Shoot", nameFold: "shoot", ownerSub: "owner-sub",
    personal: false, status: "active", memberCount: 1, createdAt: "2026-01-01T00:00:00Z",
  });
  put(`PROJECT#${PID}`, `MEMBER#${OWNER}`, { role: "member", display: "a@b", addedAt: "x", addedBy: "y" });
  put(`USER#${OWNER}`, `PROJECT#${PID}`, { role: "member" });
  if (opts.registered !== false) {
    const sk = "SRC#" + createHash("sha256").update(`${M2C_BUCKET}/${M2C_KEY}`, "utf8").digest("hex");
    put(`PROJECT#${PID}`, sk, { assetId: "video:asset-1" });
  }
  return items;
}

/** DynamoDB stand-in over `items`: BatchGetItem by key, Query by PK + `begins_with(SK, …)`. */
function ddbOver(items: Map<string, any>) {
  return async (cmd: any) => {
    if (cmd.__type === "BatchGet") {
      const keys = cmd.input.RequestItems[TABLE].Keys as Array<{ PK: string; SK: string }>;
      const found = keys.map((k) => items.get(`${k.PK}|${k.SK}`)).filter(Boolean);
      return { Responses: { [TABLE]: found } };
    }
    if (cmd.__type === "Query") {
      const vals = cmd.input.ExpressionAttributeValues as Record<string, string>;
      const [pk, prefix] = Object.values(vals);
      return { Items: [...items.values()].filter((i) => i.PK === pk && (!prefix || String(i.SK).startsWith(prefix))) };
    }
    if (cmd.__type === "Get") return {}; // session rows: none
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
  process.env.MEDIA_PROXY_BUCKET = M2C_BUCKET;
  process.env.PROJECTS_TABLE = TABLE;
  delete process.env.ARTIFACTS_REQUIRE_AUTH;
  resetProjectsStore(); // `projectsEnabled()` memoizes per module, not per app
  h.ddbSend.mockReset();
  h.ddbSend.mockImplementation(ddbOver(projectsTable()));
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

function listen(): Promise<string> {
  return listenLoopback(buildApp(), servers);
}

const art = (base: string, key: string, init?: RequestInit, extra = "") =>
  fetch(`${base}/api/artifact?key=${encodeURIComponent(key)}${extra}`, init);
const bearer = (t: string) => ({ headers: { authorization: `Bearer ${t}` } });
const urls = (base: string, keys: string[], token = OWNER) =>
  fetch(`${base}/api/artifact-urls`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify({ keys }),
  });

const s3Keys = () =>
  h.s3Send.mock.calls.filter((c) => c[0].__type === "GetObj").map((c) => `${c[0].input.Bucket}/${c[0].input.Key}`);

describe("GET /api/artifact — M2C references", () => {
  it("streams the object from the M2C PROXY bucket for a project member", async () => {
    const r = await art(await listen(), REF, bearer(OWNER));
    expect(r.status).toBe(200);
    expect(await r.text()).toBe(BODY);
    expect(s3Keys()).toEqual([`${M2C_BUCKET}/${M2C_KEY}`]);
    expect(r.headers.get("content-disposition")).toContain('filename="transcoded.mp4"');
  });

  it("is NEVER public: ungated mode still requires a token (401, no S3)", async () => {
    // Every other artifact key is public when ARTIFACTS_REQUIRE_AUTH is unset; a reference is not,
    // because the gate is project membership and that needs a verified caller.
    const r = await art(await listen(), REF);
    expect(r.status).toBe(401);
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("refuses a non-member (400, no S3) — the uniform refusal, never 403/404", async () => {
    const r = await art(await listen(), REF, bearer("intruder"));
    expect(r.status).toBe(400);
    expect(r.headers.get("content-type")).toContain("application/json");
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("refuses a location no project registered", async () => {
    h.ddbSend.mockImplementation(ddbOver(projectsTable({ registered: false })));
    const r = await art(await listen(), REF, bearer(OWNER));
    expect(r.status).toBe(400);
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("refuses a reference to any bucket other than the configured M2C proxy bucket", async () => {
    const r = await art(await listen(), `artifacts/m2c/some-other-bucket/${M2C_KEY}`, bearer(OWNER));
    expect(r.status).toBe(400);
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("refuses every reference when no projects store is configured (local dev)", async () => {
    delete process.env.PROJECTS_TABLE;
    resetProjectsStore();
    const r = await art(await listen(), REF, bearer(OWNER));
    expect(r.status).toBe(400);
    expect(h.s3Send).not.toHaveBeenCalled();
  });

  it("still serves ordinary artifact keys from OUR bucket, publicly, unchanged", async () => {
    const r = await art(await listen(), "artifacts/sess/output/a.mp4");
    expect(r.status).toBe(200);
    await r.text();
    expect(s3Keys()).toEqual(["test-bucket/artifacts/sess/output/a.mp4"]);
  });

  it("applies the same rules in gated mode", async () => {
    process.env.ARTIFACTS_REQUIRE_AUTH = "true";
    const base = await listen();
    const ok = await art(base, REF, bearer(OWNER));
    expect(ok.status).toBe(200);
    await ok.text();
    expect(s3Keys()).toEqual([`${M2C_BUCKET}/${M2C_KEY}`]);
    expect((await art(base, REF, bearer("intruder"))).status).toBe(400);
  });
});

describe("POST /api/artifact-urls — M2C references", () => {
  it("presigns the M2C proxy bucket under the referenced key for a member", async () => {
    const r = await urls(await listen(), [REF]);
    expect(r.status).toBe(200);
    const body = (await r.json()) as any;
    expect(body.urls[REF].url).toBe(`https://s3.example/${M2C_BUCKET}/${M2C_KEY}?X-Amz-Signature=sig`);
    expect(body.errors).toEqual({});
  });

  it("refuses a non-member and an unregistered location, and signs nothing", async () => {
    const unregistered = `artifacts/m2c/${M2C_BUCKET}/never/registered.mp4`;
    const r = await urls(await listen(), [REF, unregistered], "intruder");
    const body = (await r.json()) as any;
    expect(body.urls).toEqual({});
    expect(body.errors).toEqual({ [REF]: { code: "not_found" }, [unregistered]: { code: "not_found" } });
  });

  it("resolves a reference alongside ordinary keys in one batch", async () => {
    const r = await urls(await listen(), [REF, "artifacts/durable/legacy.mp4"]);
    const body = (await r.json()) as any;
    // An existing durable mirror still resolves — old manifests keep working.
    expect(body.urls["artifacts/durable/legacy.mp4"].url).toContain("test-bucket/artifacts/durable/legacy.mp4");
    expect(body.urls[REF].url).toContain(`${M2C_BUCKET}/${M2C_KEY}`);
  });
});

describe("M2C references in a FRAME — view tickets (finding #5: no access token in a frame URL)", () => {
  const frameUrls = (base: string, keys: string[], token = OWNER) =>
    fetch(`${base}/api/artifact-urls`, {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: JSON.stringify({ keys, frame: true }),
    });

  it("{frame:true} resolves a reference to a same-origin ticket URL that opens it with no header", async () => {
    const base = await listen();
    const body = (await (await frameUrls(base, [REF])).json()) as any;
    const url: string = body.urls[REF].url;
    expect(url.startsWith(`/api/artifact?key=${encodeURIComponent(REF)}&view=v1.`)).toBe(true);
    const r = await fetch(`${base}${url}`);
    expect(r.status).toBe(200);
    expect(await r.text()).toBe(BODY);
    expect(s3Keys()).toEqual([`${M2C_BUCKET}/${M2C_KEY}`]);
    expect(r.headers.get("x-content-type-options")).toBe("nosniff");
  });

  it("a ticket still needs membership at read time, and a forged ticket is refused before S3", async () => {
    const base = await listen();
    const url: string = ((await (await frameUrls(base, [REF])).json()) as any).urls[REF].url;
    // Membership revoked after the mint: the ticket names the viewer, the read re-checks.
    h.ddbSend.mockImplementation(ddbOver(projectsTable({ registered: false })));
    const revoked = await fetch(`${base}${url}`);
    expect(revoked.status).toBe(400);
    await revoked.text();
    const forged = await art(base, REF, undefined, "&view=v1.9999999999.YQ.AAAA");
    expect(forged.status).toBe(401);
    await forged.text();
    expect(h.s3Send).not.toHaveBeenCalled();
  });
});
