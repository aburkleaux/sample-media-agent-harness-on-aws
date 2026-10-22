// Integration tests for the external invoke endpoint wiring
// (POST /invoke/:agentId) — task 9.4.
//
// These mount the REAL route handler produced by `createInvokeHandler`
// (the exact factory `ui/server/app.ts` registers at
// `app.post("/invoke/:agentId", ...)`) on a throwaway Express app + HTTP
// server, and drive it over the wire with `fetch`. The AWS-touching deps
// (AssetIndex resolver, per-agent OIDC verifier, SigV4 SSE dispatcher) are
// injected as fakes so the tests are deterministic and hit no network/AWS.
//
// Coverage:
//   * 5.1 — endpoint exists and a successful path responds with
//           `content-type: text/event-stream`.
//   * 5.7 — on a valid token + resolved current published version, dispatch
//           targets the shared task-agent invocations URL with the two-identity
//           body stamping (actor_id = owner, principal = {issuer, sub}) and the
//           upstream SSE frames pass through to the client.
//   * 5.8 — when the mocked dispatch/upstream errors, a terminal SSE
//           `event: error` frame is written and the connection closes.
//
// Validates: Requirements 5.1, 5.7, 5.8

import { describe, it, expect, afterEach, vi } from "vitest";
import { closeServers, listenLoopback } from "../testing/loopback";
import express from "express";
import http from "http";
import { PassThrough } from "stream";

import {
  createInvokeHandler,
  type InvokeHandlerDeps,
  type SignedRequestStreamFn,
} from "./invokeRoute";

// ---------------------------------------------------------------------------
// Fixtures & helpers
// ---------------------------------------------------------------------------

const TASK_URL =
  "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/task-arn/invocations?qualifier=DEFAULT";

const ISSUER = "https://issuer.example.com";
const AUDIENCE = "urn:my-agent";
const OWNER = "owner-user-123";
const CALLER_SUB = "caller-sub-789";

/** A resolved current-published AssetIndex row with external integration on. */
function makeAgentRow(overrides: Record<string, any> = {}): Record<string, any> {
  return {
    ownerUserId: OWNER,
    version: 4,
    compatibleSkills: ["skill-a", "skill-b"],
    toolCollections: ["tc-1"],
    model: "us.anthropic.claude-sonnet-4",
    integrationConfig: {
      enabled: true,
      issuer: ISSUER,
      audience: AUDIENCE,
      allowedClientIds: [],
    },
    ...overrides,
  };
}

/**
 * A fake upstream `IncomingMessage`: a PassThrough carrying `statusCode` +
 * `headers`, that (asynchronously, after the handler has attached its
 * data/end/error listeners) emits the supplied SSE chunks and then either
 * ends cleanly or emits a mid-stream `error`.
 */
function makeUpstream(
  chunks: string[],
  opts: { contentType?: string; errorAfter?: boolean } = {},
): PassThrough {
  const pt = new PassThrough() as PassThrough & {
    statusCode?: number;
    headers?: Record<string, string>;
  };
  pt.statusCode = 200;
  pt.headers = { "content-type": opts.contentType ?? "text/event-stream" };
  setImmediate(() => {
    for (const c of chunks) pt.write(c);
    if (opts.errorAfter) {
      pt.emit("error", new Error("simulated upstream stream failure"));
    } else {
      pt.end();
    }
  });
  return pt;
}

/** Default happy-path deps; individual tests override the pieces they exercise. */
function makeDeps(overrides: Partial<InvokeHandlerDeps> = {}): InvokeHandlerDeps {
  return {
    invokeTaskUrl: TASK_URL,
    assetIndexReady: true,
    resolveCurrentPublishedAgent: async () => makeAgentRow(),
    verifyAgentToken: async () => ({ sub: CALLER_SUB, clientId: null }),
    signedRequestStream: async () => makeUpstream(["event: text\ndata: hi\n\n"]) as any,
    ...overrides,
  };
}

const servers: http.Server[] = [];

/** Mount the real handler on a fresh Express app + ephemeral HTTP server. */
function mount(deps: InvokeHandlerDeps): Promise<string> {
  const app = express();
  app.post("/invoke/:agentId", createInvokeHandler(deps));
  return listenLoopback(app, servers);
}

afterEach(async () => {
  await closeServers(servers);
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// 5.1 — endpoint existence + SSE content type
// ---------------------------------------------------------------------------

describe("POST /invoke/:agentId — endpoint existence + SSE content type (Req 5.1)", () => {
  it("exposes the route and streams a successful response as text/event-stream", async () => {
    const base = await mount(makeDeps());

    const res = await fetch(`${base}/invoke/agent-xyz`, {
      method: "POST",
      headers: {
        authorization: "Bearer good.jwt.token",
        "content-type": "application/json",
      },
      body: JSON.stringify({ prompt: "hello" }),
    });

    // Route matched (not a bare Express 404) and opened an SSE stream.
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("text/event-stream");

    const text = await res.text();
    expect(text).toContain("event: text");
  });
});

// ---------------------------------------------------------------------------
// 5.7 — SigV4 dispatch to the shared ARN + two-identity stamping + passthrough
// ---------------------------------------------------------------------------

describe("POST /invoke/:agentId — dispatch + body stamping + SSE passthrough (Req 5.7)", () => {
  it("dispatches to the shared task-agent URL with two-identity stamping and passes SSE frames through", async () => {
    const captured: { url: string; body: Buffer; headers?: Record<string, string> }[] = [];

    const upstreamFrames = [
      "event: step\ndata: {\"n\":1}\n\n",
      "event: text\ndata: streamed-token\n\n",
    ];

    const signedRequestStream: SignedRequestStreamFn = async (url, body, headers) => {
      captured.push({ url, body, headers });
      return makeUpstream(upstreamFrames) as any;
    };

    const base = await mount(
      makeDeps({
        resolveCurrentPublishedAgent: async () => makeAgentRow(),
        verifyAgentToken: async () => ({ sub: CALLER_SUB, clientId: "client-1" }),
        signedRequestStream,
      }),
    );

    const res = await fetch(`${base}/invoke/agent-xyz`, {
      method: "POST",
      headers: {
        authorization: "Bearer good.jwt.token",
        "content-type": "application/json",
      },
      // Caller attempts to smuggle identity — must be stripped, never honored.
      body: JSON.stringify({ prompt: "run it", actor_id: "attacker", principal: "spoof" }),
    });

    const text = await res.text();

    // (a) Dispatch happened exactly once, against the SHARED task-agent URL.
    expect(captured).toHaveLength(1);
    expect(captured[0].url).toBe(TASK_URL);

    // (b) The runtime session id travels on the dispatch header.
    expect(captured[0].headers?.["x-amzn-bedrock-agentcore-runtime-session-id"]).toMatch(/^EXT_/);

    // (c) Two-identity body stamping.
    const outbound = JSON.parse(captured[0].body.toString("utf8"));
    expect(outbound.actor_id).toBe(OWNER); // resource actor = Owner
    expect(outbound.principal).toEqual({ issuer: ISSUER, sub: CALLER_SUB }); // End_User only
    expect(outbound.session_id).toMatch(/^EXT_/);
    expect(outbound.actor_groups).toEqual([]);
    // Caller-supplied identity fields were stripped, not honored.
    expect(outbound.actor_id).not.toBe("attacker");
    expect(outbound.principal).not.toBe("spoof");
    // Non-reserved caller fields preserved; the published-version REFERENCE
    // (not config content) is forwarded — the Task Agent resolves persona/
    // skills/tools/model itself from the AssetIndex + lab artifacts bucket
    // (custom-agents seam fix; see invokeRoute.pipeline.test.ts §3 and
    // agent/tests/test_task_agent_external_config.py).
    expect(outbound.prompt).toBe("run it");
    expect(outbound.agent_id).toBe("agent-xyz");
    expect(outbound.version).toBe(4); // makeAgentRow() version
    expect(outbound).not.toHaveProperty("skills");
    expect(outbound).not.toHaveProperty("tool_collections");
    expect(outbound).not.toHaveProperty("model_id");
    expect(outbound).not.toHaveProperty("custom_agent");
    expect(outbound).not.toHaveProperty("system_prompt");
    expect(outbound).not.toHaveProperty("tools");

    // (d) SSE passthrough — both upstream frames reached the client.
    expect(res.headers.get("content-type")).toBe("text/event-stream");
    for (const frame of upstreamFrames) {
      expect(text).toContain(frame.trim());
    }
  });
});

// ---------------------------------------------------------------------------
// 5.8 — terminal SSE error on upstream failure
// ---------------------------------------------------------------------------

describe("POST /invoke/:agentId — terminal SSE error on upstream failure (Req 5.8)", () => {
  it("emits a terminal SSE error frame and closes when dispatch cannot be established", async () => {
    const base = await mount(
      makeDeps({
        signedRequestStream: async () => {
          throw new Error("connect ECONNREFUSED");
        },
      }),
    );

    const res = await fetch(`${base}/invoke/agent-xyz`, {
      method: "POST",
      headers: { authorization: "Bearer good.jwt.token" },
      body: JSON.stringify({ prompt: "hi" }),
    });

    // The SSE stream is opened, then a terminal error frame closes it.
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("text/event-stream");

    const text = await res.text();
    expect(text).toContain("event: error");
    expect(text).toContain("upstream_error");
    // Connection closed (fetch resolved text() to completion).
  });

  it("emits a terminal SSE error frame when the upstream stream errors mid-flight", async () => {
    const base = await mount(
      makeDeps({
        // Deliver one frame, then blow up the stream.
        signedRequestStream: async () =>
          makeUpstream(["event: text\ndata: partial\n\n"], { errorAfter: true }) as any,
      }),
    );

    const res = await fetch(`${base}/invoke/agent-xyz`, {
      method: "POST",
      headers: { authorization: "Bearer good.jwt.token" },
      body: JSON.stringify({ prompt: "hi" }),
    });

    expect(res.status).toBe(200);
    const text = await res.text();
    // Partial frame delivered, then the terminal error frame on failure.
    expect(text).toContain("partial");
    expect(text).toContain("event: error");
  });
});
