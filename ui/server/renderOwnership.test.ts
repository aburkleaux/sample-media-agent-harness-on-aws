// Security regression: GET /sessions/:id/renders must enforce session ownership (parity with
// /transcript). The render manifests it lists embed the session's media URLs/keys, so a caller must
// only ever read their OWN session's renders — never another actor's by guessing a session id.
//
// There was previously NO HTTP-level test for the GET endpoint (only the DELETE and the pure
// composeRenderTabs were covered), so this file both locks the ownership gate and gives the GET path
// its first coverage. Mocks DynamoDB (the session-row ownership read) and S3 (the manifest list/get).
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import http from "http";

const OWNER = "local-dev-user"; // what DEV_BYPASS_AUTH + LAB_DEFAULT_ACTOR_ID resolves req.actorId to

// The session-row ownership read. Each test sets what the GetCommand returns.
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

// S3: one render manifest under the session's render prefix.
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
  process.env.SESSIONS_TABLE = "test-sessions";
  process.env.DEV_BYPASS_AUTH = "true";
  process.env.LAB_DEFAULT_ACTOR_ID = OWNER;
  ddbSend.mockReset();
  s3Send.mockReset();
  // S3 always has one manifest available; the ownership gate (not S3) decides whether it is served.
  s3Send.mockImplementation(async (cmd: any) => {
    if (cmd.__type === "List") {
      return { Contents: [{ Key: "artifacts/sess-1/render/m1.json", LastModified: new Date("2026-01-01T00:00:00Z") }] };
    }
    if (cmd.__type === "GetObj") {
      return {
        Body: {
          transformToString: async () =>
            JSON.stringify({ id: "m1", renderSkill: "render-shot-segmentation", template: "generic-preview" }),
        },
      };
    }
    return {};
  });
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

function listen(): Promise<string> {
  return listenLoopback(buildApp(), servers);
}

const getRenders = (base: string, id: string) => fetch(`${base}/sessions/${id}/renders`);

describe("GET /sessions/:id/renders — ownership gate", () => {
  it("lists renders when the caller OWNS the session", async () => {
    ddbSend.mockResolvedValue({ Item: { sessionId: "sess-1", actorId: OWNER } });
    const r = await getRenders(await listen(), "sess-1");
    expect(r.status).toBe(200);
    const body = await r.json();
    expect(Array.isArray(body.renders)).toBe(true);
    expect(body.renders.length).toBe(1);
    // S3 was consulted only because ownership passed.
    expect(s3Send).toHaveBeenCalled();
  });

  it("returns an EMPTY list for a session owned by a DIFFERENT actor (no cross-actor read)", async () => {
    ddbSend.mockResolvedValue({ Item: { sessionId: "sess-1", actorId: "someone-else" } });
    const r = await getRenders(await listen(), "sess-1");
    expect(r.status).toBe(200);
    const body = await r.json();
    expect(body.renders).toEqual([]);
    // Must not have reached S3 — the gate denied before listing.
    expect(s3Send).not.toHaveBeenCalled();
  });

  it("returns an EMPTY list for an unknown / orphaned session row (no actorId)", async () => {
    ddbSend.mockResolvedValue({}); // no Item
    const r = await getRenders(await listen(), "sess-1");
    expect(r.status).toBe(200);
    const body = await r.json();
    expect(body.renders).toEqual([]);
    expect(s3Send).not.toHaveBeenCalled();
  });

  it("returns an EMPTY list (fails closed) when the ownership lookup errors", async () => {
    ddbSend.mockRejectedValue(new Error("ddb down"));
    const r = await getRenders(await listen(), "sess-1");
    expect(r.status).toBe(200);
    const body = await r.json();
    expect(body.renders).toEqual([]);
    expect(s3Send).not.toHaveBeenCalled();
  });
});

describe("GET /sessions/:id/renders — tab labels", () => {
  it("names a tab after its skill, else after the manifest's own title (show_asset's file name)", async () => {
    ddbSend.mockResolvedValue({ Item: { sessionId: "sess-1", actorId: OWNER } });
    s3Send.mockImplementation(async (cmd: any) => {
      if (cmd.__type === "List") {
        return { Contents: [
          { Key: "artifacts/sess-1/render/m1.json", LastModified: new Date("2026-01-01T00:00:00Z") },
          { Key: "artifacts/sess-1/render/asset-x.json", LastModified: new Date("2026-01-01T00:00:01Z") },
        ] };
      }
      if (cmd.__type === "GetObj") {
        const body = String(cmd.input.Key).endsWith("asset-x.json")
          ? { id: "asset-x", template: "asset-player", title: "sam.webp", kind: "image",
              ref: { project_id: "p1", asset_id: "image:1", frame_s3_key: null }, displayName: "sam.webp", startMs: 0 }
          : { id: "m1", renderSkill: "render-shot-segmentation", template: "generic-preview", title: "ignored" };
        return { Body: { transformToString: async () => JSON.stringify(body) } };
      }
      return {};
    });
    const body = await (await getRenders(await listen(), "sess-1")).json();
    const labels = body.renders.map((r: any) => r.label);
    expect(labels).toEqual(["Shot Segmentation", "sam.webp"]);
  });
});
