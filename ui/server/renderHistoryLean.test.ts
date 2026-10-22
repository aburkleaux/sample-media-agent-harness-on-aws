// A composite render block inlines the FULL manifest object into block.data.manifest (~471 KB
// observed). renderHistory shares the session row with the chat transcript, so one inlined
// manifest pushed the row past DynamoDB's 400 KB item limit and made EVERY write to the row fail
// — silently dropping chat turns (gone on refresh). The proper fix persists renderHistory as LEAN
// refs: strip the inlined manifest, keep manifestUrl / sourceKey / memberKeys. S3 manifests are
// authoritative and re-derive renders on load.
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

import { buildApp, leanRenderHistory, byteCapTranscript } from "./app";
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

const bigManifest = { tracks: Array.from({ length: 5000 }, (_, i) => ({ id: i, note: "x".repeat(50) })) };

describe("leanRenderHistory", () => {
  it("drops the inlined manifest but keeps lean refs, collapsing size", () => {
    const heavy = [{
      label: "Timeline",
      timestamp: 1,
      blocks: [{ template: "manifest-router", title: "T", data: { manifest: bigManifest, sourceKey: "sk", memberKeys: ["k1", "k2"] } }],
    }];
    const lean = leanRenderHistory(heavy) as any[];
    expect(lean[0].blocks[0].data).not.toHaveProperty("manifest");
    expect(lean[0].blocks[0].data.sourceKey).toBe("sk");
    expect(lean[0].blocks[0].data.memberKeys).toEqual(["k1", "k2"]);
    expect(lean[0].label).toBe("Timeline");
    // A solo block's manifestUrl ref is untouched.
    const solo = leanRenderHistory([{ blocks: [{ data: { manifestUrl: "/api/artifact?key=x" } }] }]) as any[];
    expect(solo[0].blocks[0].data.manifestUrl).toBe("/api/artifact?key=x");
    // Size collapses from hundreds of KB to well under 1 KB.
    expect(JSON.stringify(lean).length).toBeLessThan(1024);
    expect(JSON.stringify(heavy).length).toBeGreaterThan(100_000);
  });

  it("keeps a composite tab's identity (key, compositeKey, memberKeys) so it re-resolves after reload", () => {
    const lean = leanRenderHistory([{
      key: "composite:asset:video:1",
      blocks: [{ template: "manifest-router", title: "T", data: {
        manifest: bigManifest, compositeKey: "composite:asset:video:1", memberKeys: ["a.json", "b.json"],
      } }],
    }]) as any[];
    expect(lean[0].key).toBe("composite:asset:video:1");
    expect(lean[0].blocks[0].data).toEqual({ compositeKey: "composite:asset:video:1", memberKeys: ["a.json", "b.json"] });
  });
});

describe("byteCapTranscript", () => {
  it("drops oldest messages until under the byte budget, keeping the most recent", () => {
    // 20 messages of ~2 KB each = ~40 KB; cap at 10 KB should keep only the newest few.
    const msgs = Array.from({ length: 20 }, (_, i) => ({ role: i % 2 ? "assistant" : "user", content: `${i}:` + "x".repeat(2000) }));
    const capped = byteCapTranscript(msgs, 10_000) as any[];
    expect(capped.length).toBeGreaterThan(0);
    expect(capped.length).toBeLessThan(20);
    expect(Buffer.byteLength(JSON.stringify(capped), "utf8")).toBeLessThanOrEqual(10_000);
    // newest message is retained
    expect(capped[capped.length - 1].content).toContain("19:");
  });
  it("keeps a short transcript untouched and coerces non-arrays to []", () => {
    const msgs = [{ role: "user", content: "hi" }];
    expect(byteCapTranscript(msgs)).toEqual(msgs);
    expect(byteCapTranscript(undefined)).toEqual([]);
  });
});

describe("PATCH /sessions/:id — persists lean renderHistory", () => {
  it("strips inline manifests before writing so the row cannot exceed the 400 KB item limit", async () => {
    const res = await fetch(`${await listen()}/sessions/sess-1`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        renderHistory: [{
          label: "Timeline",
          blocks: [{ template: "manifest-router", data: { manifest: bigManifest, sourceKey: "sk" } }],
        }],
      }),
    });
    expect(res.status).toBe(200);
    const update = sendMock.mock.calls.map((c) => c[0]).find((c: any) => c.__type === "Update");
    const persisted = update.input.ExpressionAttributeValues[":rh"];
    expect(persisted[0].blocks[0].data).not.toHaveProperty("manifest");
    expect(persisted[0].blocks[0].data.sourceKey).toBe("sk");
    expect(JSON.stringify(persisted).length).toBeLessThan(1024); // no giant manifest on the row
  });
});
