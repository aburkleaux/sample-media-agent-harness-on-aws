// Node job ids embed a '#' (e.g. "<runId>#frame_05"), which the client percent-encodes to "%23".
// The /jobs middleware must DECODE the path segment before the DynamoDB key lookup, or every
// GET/DELETE of a node job misses the row (and on an older build with no DELETE branch, falls
// through to the SPA — surfacing in the client as `Unexpected token '<', "<!doctype "...`).
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import http from "http";

// Mock the DynamoDB document client so no AWS/network is touched (CI-safe). The command classes
// capture their `.input` so we can assert the Key.jobId the handler looked up.
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

const DECODED = "d7c87482-18e3-48f2-83c5-9ddd09593670#frame_05";
const ENCODED = encodeURIComponent(DECODED); // -> ...%23frame_05

const servers: http.Server[] = [];
const saved: Record<string, string | undefined> = {};
const VARS = ["AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "JOB_REGISTRY_TABLE", "LAB_DEFAULT_ACTOR_ID"];

beforeEach(() => {
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = "test-bucket";
  process.env.JOB_REGISTRY_TABLE = "test-jobs";
  process.env.DEV_BYPASS_AUTH = "true"; // inject X-Actor-Id without a real token
  process.env.LAB_DEFAULT_ACTOR_ID = "local-dev-user";
  sendMock.mockReset();
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

function listen(): Promise<string> {
  return listenLoopback(buildApp(), servers);
}

describe("DELETE /jobs/:jobId — node-id decode", () => {
  it("decodes %23 to # so a '<runId>#frame_05' job matches the DynamoDB key and is deleted", async () => {
    sendMock.mockImplementation((cmd: any) => {
      if (cmd.__type === "Get") {
        return Promise.resolve({ Item: { jobId: DECODED, actorId: "local-dev-user", status: "SUCCEEDED" } });
      }
      return Promise.resolve({}); // Delete
    });

    const res = await fetch(`${await listen()}/jobs/${ENCODED}?force=true`, { method: "DELETE" });

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.deleted).toEqual([DECODED]);

    // The crux: both the ownership GetItem and the DeleteItem used the DECODED id.
    const calls = sendMock.mock.calls.map((c) => c[0]);
    const getCmd = calls.find((c: any) => c.__type === "Get");
    const delCmd = calls.find((c: any) => c.__type === "Delete");
    expect(getCmd?.input?.Key?.jobId).toBe(DECODED);
    expect(delCmd?.input?.Key?.jobId).toBe(DECODED);
  });
});
