// R8.1 / R8.2: the render store accepts client-authored views. POST /sessions/:id/renders {manifest}
// writes artifacts/<sid>/render/<id>.json (so an Expand tab is listed like any render and survives
// refresh); PATCH /sessions/:id/renders?key= {viewState} merges the view's timeline state into that
// manifest. Both share DELETE's gate: owned + live session, 423 when locked, key under this
// session's render prefix. Mocks DynamoDB (the session row) and S3 (the manifest store).
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
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

// A tiny in-memory S3: objects by key, with LastModified.
const objects = new Map<string, { body: string; lastModified: Date }>();
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
let clock = Date.parse("2026-10-08T10:00:00Z");

beforeEach(() => {
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = "test-bucket";
  process.env.SESSIONS_TABLE = "test-sessions";
  process.env.DEV_BYPASS_AUTH = "true";
  process.env.LAB_DEFAULT_ACTOR_ID = OWNER;
  objects.clear();
  ddbSend.mockReset();
  ddbSend.mockResolvedValue({ Item: { sessionId: "sess-1", actorId: OWNER, active: 1 } });
  s3Send.mockReset();
  s3Send.mockImplementation(async (cmd: any) => {
    const { Key, Prefix, Body } = cmd.input;
    if (cmd.__type === "PutObj") {
      clock += 60_000;
      objects.set(Key, { body: String(Body), lastModified: new Date(clock) });
      return {};
    }
    if (cmd.__type === "GetObj") {
      const o = objects.get(Key);
      if (!o) { const e: any = new Error("NoSuchKey"); e.name = "NoSuchKey"; throw e; }
      return { Body: { transformToString: async () => o.body } };
    }
    if (cmd.__type === "List") {
      return {
        Contents: [...objects.entries()]
          .filter(([k]) => k.startsWith(Prefix))
          .map(([k, o]) => ({ Key: k, LastModified: o.lastModified })),
      };
    }
    if (cmd.__type === "DelObj") { objects.delete(Key); return {}; }
    return {};
  });
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

const listen = () => listenLoopback(buildApp(), servers);
const json = (method: string, body: unknown) => ({
  method, headers: { "content-type": "application/json" }, body: JSON.stringify(body),
});
const assetView = (over: Record<string, unknown> = {}) => ({
  id: "view-abc",
  template: "asset-player",
  createdAt: "2026-10-08T09:00:00.000Z",
  title: "nba.mp4",
  ref: { project_id: "p1", asset_id: "a1", frame_s3_key: null },
  kind: "video",
  displayName: "nba.mp4",
  startMs: 0,
  ...over,
});
const stored = (key: string) => JSON.parse(objects.get(key)!.body);
const puts = () => s3Send.mock.calls.map((c) => c[0]).filter((c: any) => c.__type === "PutObj");

describe("POST /sessions/:id/renders — persist a view", () => {
  it("writes artifacts/<sid>/render/<manifest id>.json and returns the key", async () => {
    const res = await fetch(`${await listen()}/sessions/sess-1/renders`, json("POST", { manifest: assetView() }));
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.key).toBe("artifacts/sess-1/render/view-abc.json");
    expect(stored(body.key)).toMatchObject({ id: "view-abc", template: "asset-player", ref: { asset_id: "a1" } });
  });

  it("sanitises the id into a valid render name; no id → a body hash", async () => {
    const base = await listen();
    const a = await (await fetch(`${base}/sessions/sess-1/renders`, json("POST", { manifest: assetView({ id: "../x y/..z" }) }))).json();
    expect(a.key).toMatch(/^artifacts\/sess-1\/render\/[\w.\-]+\.json$/);
    expect(a.key).not.toContain("..");
    const b = await (await fetch(`${base}/sessions/sess-1/renders`, json("POST", { manifest: assetView({ id: undefined }) }))).json();
    expect(b.key).toMatch(/^artifacts\/sess-1\/render\/view-[0-9a-f]{24}\.json$/);
  });

  it("listed by GET afterwards, so the view survives a refresh", async () => {
    const base = await listen();
    await fetch(`${base}/sessions/sess-1/renders`, json("POST", { manifest: assetView() }));
    const list = await (await fetch(`${base}/sessions/sess-1/renders`)).json();
    expect(list.renders.map((r: any) => r.key)).toEqual(["artifacts/sess-1/render/view-abc.json"]);
    expect(list.renders[0].label).toBe("nba.mp4");
  });

  it("refuses a template outside the allowlist, and a body over 256 KB", async () => {
    const base = await listen();
    const bad = await fetch(`${base}/sessions/sess-1/renders`, json("POST", { manifest: { id: "x", template: "document-viewer" } }));
    expect(bad.status).toBe(400);
    const huge = await fetch(`${base}/sessions/sess-1/renders`, json("POST", { manifest: assetView({ pad: "x".repeat(300 * 1024) }) }));
    expect(huge.status).toBe(413);
    expect(puts()).toHaveLength(0);
  });

  it("404s in another actor's session and 423s in a locked one — writing nothing", async () => {
    const base = await listen();
    ddbSend.mockResolvedValue({ Item: { sessionId: "sess-1", actorId: "intruder" } });
    expect((await fetch(`${base}/sessions/sess-1/renders`, json("POST", { manifest: assetView() }))).status).toBe(404);
    ddbSend.mockResolvedValue({ Item: { sessionId: "sess-1", actorId: OWNER, locked: true } });
    expect((await fetch(`${base}/sessions/sess-1/renders`, json("POST", { manifest: assetView() }))).status).toBe(423);
    ddbSend.mockResolvedValue({ Item: { sessionId: "sess-1", actorId: OWNER, active: 0 } });
    expect((await fetch(`${base}/sessions/sess-1/renders`, json("POST", { manifest: assetView() }))).status).toBe(404);
    expect(puts()).toHaveLength(0);
  });
});

describe("PATCH /sessions/:id/renders?key= — merge viewState", () => {
  const KEY = "artifacts/sess-1/render/view-abc.json";
  const patch = (base: string, key: string, viewState: unknown) =>
    fetch(`${base}/sessions/sess-1/renders?key=${encodeURIComponent(key)}`, json("PATCH", { viewState }));

  it("merges range / frameMs / crop into the manifest and keeps everything else", async () => {
    const base = await listen();
    await fetch(`${base}/sessions/sess-1/renders`, json("POST", { manifest: assetView() }));
    const r1 = await patch(base, KEY, { range: { start: 2, end: 5.5 }, frameMs: 3000 });
    expect(r1.status).toBe(200);
    const r2 = await patch(base, KEY, { crop: { x: 0.1, y: 0.2, w: 0.5, h: 0.5 }, frameMs: null });
    expect((await r2.json()).viewState).toEqual({ range: { start: 2, end: 5.5 }, crop: { x: 0.1, y: 0.2, w: 0.5, h: 0.5 } });
    const m = stored(KEY);
    expect(m.viewState).toEqual({ range: { start: 2, end: 5.5 }, crop: { x: 0.1, y: 0.2, w: 0.5, h: 0.5 } });
    expect(m).toMatchObject({ id: "view-abc", template: "asset-player", displayName: "nba.mp4" });
  });

  it("400s an invalid viewState, a foreign key, and an unknown field", async () => {
    const base = await listen();
    await fetch(`${base}/sessions/sess-1/renders`, json("POST", { manifest: assetView() }));
    expect((await patch(base, KEY, { range: { start: 5, end: 2 } })).status).toBe(400);
    expect((await patch(base, KEY, { crop: { x: 0.8, y: 0, w: 0.5, h: 0.5 } })).status).toBe(400);
    expect((await patch(base, KEY, { zoom: 3 })).status).toBe(400);
    expect((await patch(base, "artifacts/OTHER/render/view-abc.json", { frameMs: 1 })).status).toBe(400);
    expect(stored(KEY).viewState).toBeUndefined();
  });

  it("404s a manifest that does not exist; 423s a locked session", async () => {
    const base = await listen();
    expect((await patch(base, "artifacts/sess-1/render/nope.json", { frameMs: 1 })).status).toBe(404);
    await fetch(`${base}/sessions/sess-1/renders`, json("POST", { manifest: assetView() }));
    ddbSend.mockResolvedValue({ Item: { sessionId: "sess-1", actorId: OWNER, locked: true } });
    expect((await patch(base, KEY, { frameMs: 1 })).status).toBe(423);
    expect(stored(KEY).viewState).toBeUndefined();
  });

  it("a PATCHed view keeps its place in the list (ordered by its own createdAt, not the rewrite)", async () => {
    const base = await listen();
    await fetch(`${base}/sessions/sess-1/renders`, json("POST", { manifest: assetView({ id: "first", createdAt: "2026-10-08T09:00:00.000Z" }) }));
    await fetch(`${base}/sessions/sess-1/renders`, json("POST", { manifest: assetView({ id: "second", createdAt: "2026-10-08T09:30:00.000Z" }) }));
    await patch(base, "artifacts/sess-1/render/first.json", { frameMs: 1000 });
    const list = await (await fetch(`${base}/sessions/sess-1/renders`)).json();
    expect(list.renders.map((r: any) => r.key)).toEqual([
      "artifacts/sess-1/render/first.json",
      "artifacts/sess-1/render/second.json",
    ]);
  });
});
