// Security regression: POST /render must enforce the SAME session-ownership rule as /api.
//
// The render session decides where the Render Agent writes its manifest (`artifacts/<session>/render/…`)
// and which runtime session it runs in. It came from the request body (`session_id`) or from the session a
// URL names — both caller-controlled — and was used unchecked, so any signed-in user could write a render
// manifest into another user's session folder. These tests drive the real route against a loopback stub of
// the Render Agent (LOCAL_RENDER_AGENT_URL, plain http, no SigV4) and assert which session it was invoked on.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import http from "http";
import type { AddressInfo } from "net";

const OWNER = "local-dev-user"; // DEV_BYPASS_AUTH + LAB_DEFAULT_ACTOR_ID
const OWN_SID = "11111111-1111-4111-8111-111111111111";
const OTHER_SID = "22222222-2222-4222-8222-222222222222";
const LOCKED_SID = "33333333-3333-4333-8333-333333333333";
const FRESH_SID = "44444444-4444-4444-8444-444444444444"; // client-minted, no row yet

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

import { buildApp } from "./app";
import { closeServers, listenLoopback, LOOPBACK_HOST } from "./testing/loopback";
import { artifactOwnerTag, ownerTagOfRenderSession } from "./artifactOwner";

const servers: http.Server[] = [];
const VARS = ["AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "LAB_DEFAULT_ACTOR_ID", "SESSIONS_TABLE",
  "LOCAL_RENDER_AGENT_URL"];
const saved: Record<string, string | undefined> = {};

/** Sessions the render agent stub was invoked on (the runtime-session header). */
let invokedOn: string[] = [];
let stub: http.Server | undefined;

function rows(cmd: any) {
  const sid = cmd.input?.Key?.sessionId;
  if (sid === OWN_SID) return { Item: { actorId: OWNER } };
  if (sid === OTHER_SID) return { Item: { actorId: "someone-else" } };
  if (sid === LOCKED_SID) return { Item: { actorId: OWNER, locked: true } };
  if (sid === "ddb-down-0000000000000000000000000000") throw new Error("ddb down");
  return {};
}

beforeEach(async () => {
  invokedOn = [];
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = "test-bucket";
  process.env.DEV_BYPASS_AUTH = "true";
  process.env.LAB_DEFAULT_ACTOR_ID = OWNER;
  process.env.SESSIONS_TABLE = "test-sessions";
  ddbSend.mockReset();
  ddbSend.mockImplementation(async (cmd: any) => rows(cmd));
  stub = http.createServer((req, res) => {
    invokedOn.push(String(req.headers["x-amzn-bedrock-agentcore-runtime-session-id"] ?? ""));
    req.resume();
    req.on("end", () => { res.writeHead(200, { "content-type": "text/plain" }); res.end("no manifest here"); });
  });
  await new Promise<void>((r) => stub!.listen(0, LOOPBACK_HOST, r));
  process.env.LOCAL_RENDER_AGENT_URL = `http://${LOOPBACK_HOST}:${(stub.address() as AddressInfo).port}/invocations`;
});
afterEach(async () => {
  await closeServers(servers);
  await new Promise<void>((r) => { stub?.closeAllConnections(); stub?.close(() => r()); });
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

async function render(body: Record<string, unknown>) {
  const base = await listenLoopback(buildApp(), servers);
  const r = await fetch(`${base}/render`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await r.text();
  let json: any = null;
  try { json = JSON.parse(text); } catch { /* not JSON */ }
  return { status: r.status, json };
}

const URL_OF = (sid: string) => `/api/artifact?key=${encodeURIComponent(`artifacts/${sid}/output/clip.mp4`)}`;

describe("POST /render — session ownership (parity with /api)", () => {
  it("renders into a session the caller OWNS, named by the body", async () => {
    await render({ url: URL_OF(OWN_SID), session_id: OWN_SID });
    expect(invokedOn).toEqual([OWN_SID]);
  });

  it("403s a body session_id owned by ANOTHER user, and never invokes the Render Agent", async () => {
    const { status, json } = await render({ url: URL_OF(OTHER_SID), session_id: OTHER_SID });
    expect(status).toBe(403);
    expect(json.error.code).toBe("session_not_owned");
    expect(invokedOn).toEqual([]);
  });

  it("400s a body session_id that is not a chat session id (an actor-derived runtime session)", async () => {
    const { status, json } = await render({ url: URL_OF(OWN_SID), session_id: `lab-${"a".repeat(64)}` });
    expect(status).toBe(400);
    expect(json.error.code).toBe("invalid_session_id");
    expect(invokedOn).toEqual([]);
  });

  it("423s a locked session", async () => {
    const { status } = await render({ url: URL_OF(LOCKED_SID), session_id: LOCKED_SID });
    expect(status).toBe(423);
    expect(invokedOn).toEqual([]);
  });

  it("503s (fails closed) when the ownership lookup errors", async () => {
    const { status } = await render({ url: URL_OF(OWN_SID), session_id: "ddb-down-0000000000000000000000000000" });
    expect(status).toBe(503);
    expect(invokedOn).toEqual([]);
  });

  it("uses the session the URL names only when the caller owns it", async () => {
    await render({ url: URL_OF(OWN_SID) });
    expect(invokedOn).toEqual([OWN_SID]);
  });

  it("never renders into ANOTHER user's session named by the URL: a fresh owner-tagged session instead", async () => {
    await render({ url: URL_OF(OTHER_SID) });
    expect(invokedOn).toHaveLength(1);
    expect(invokedOn[0]).not.toBe(OTHER_SID);
    expect(ownerTagOfRenderSession(invokedOn[0])).toBe(artifactOwnerTag(OWNER));
  });

  it("renders a body session with no row yet into an owner-tagged session (nothing is written to an unclaimed id)", async () => {
    await render({ url: URL_OF(FRESH_SID), session_id: FRESH_SID });
    expect(invokedOn).toHaveLength(1);
    expect(invokedOn[0]).not.toBe(FRESH_SID);
    expect(ownerTagOfRenderSession(invokedOn[0])).toBe(artifactOwnerTag(OWNER));
    expect(invokedOn[0].length).toBeGreaterThanOrEqual(33); // AgentCore's runtime-session minimum
  });

  it("tags a session-less render (an external URL) with the caller", async () => {
    await render({ url: "https://example.com/video.mp4" });
    expect(ownerTagOfRenderSession(invokedOn[0])).toBe(artifactOwnerTag(OWNER));
  });
});
