// Integration tests for the external invoke path's streaming transport
// reliability guardrails (POST /invoke/:agentId) — task 15.2, Req 15.1/15.2/15.4.
//
// These mount the REAL handler produced by `createInvokeHandler` on a throwaway
// Express app + ephemeral HTTP server (same harness style as
// `invokeRoute.test.ts`) and drive it over the wire, injecting fakes for the
// AWS-touching deps plus the optional `streamGuard`/`transport` deps under test.
//
// Coverage:
//   * 15.4 — with a `streamGuard` of cap 1, a first (held-open) stream reserves
//            the only slot; a concurrent second request is rejected with HTTP
//            503 `service_unavailable` + `retry-after`; once the first upstream
//            ends the slot is released and a later request is admitted again.
//   * 15.2 — a streaming response carries `x-accel-buffering: no` and
//            `content-type: text/event-stream` (buffering disabled), and the
//            `flushHeaders` path does not error.
//   * 15.1 — an idle open stream emits a `: keep-alive` SSE comment frame before
//            any real frame, using a small keepalive cadence via the transport
//            dep (the config invariant itself is asserted in streamGuard.test.ts).
//
// Validates: Requirements 15.1, 15.2, 15.4

import { describe, it, expect, afterEach, vi } from "vitest";
import { closeServers, listenLoopback } from "../testing/loopback";
import express from "express";
import http from "http";
import { PassThrough } from "stream";

import {
  createInvokeHandler,
  type InvokeHandlerDeps,
} from "./invokeRoute";
import {
  createStreamGuard,
  DEFAULT_TRANSPORT_CONFIG,
  type TransportConfig,
} from "./streamGuard";

// ---------------------------------------------------------------------------
// Fixtures & helpers
// ---------------------------------------------------------------------------

const TASK_URL =
  "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/task-arn/invocations?qualifier=DEFAULT";

const ISSUER = "https://issuer.example.com";
const AUDIENCE = "urn:my-agent";
const OWNER = "owner-user-123";
const CALLER_SUB = "caller-sub-789";

function makeAgentRow(overrides: Record<string, any> = {}): Record<string, any> {
  return {
    ownerUserId: OWNER,
    version: 4,
    compatibleSkills: ["skill-a"],
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
 * A fake upstream `IncomingMessage` that stays OPEN until `finish()` is called,
 * so the handler holds its concurrency slot / keeps the SSE response open. The
 * returned controls let a test emit frames and end the stream on demand.
 */
function makeControllableUpstream(opts: { contentType?: string } = {}) {
  const pt = new PassThrough() as PassThrough & {
    statusCode?: number;
    headers?: Record<string, string>;
  };
  pt.statusCode = 200;
  pt.headers = { "content-type": opts.contentType ?? "text/event-stream" };
  return {
    stream: pt,
    emitFrame: (frame: string) => pt.write(frame),
    finish: () => pt.end(),
  };
}

function makeDeps(overrides: Partial<InvokeHandlerDeps> = {}): InvokeHandlerDeps {
  return {
    invokeTaskUrl: TASK_URL,
    assetIndexReady: true,
    resolveCurrentPublishedAgent: async () => makeAgentRow(),
    verifyAgentToken: async () => ({ sub: CALLER_SUB, clientId: null }),
    signedRequestStream: async () => {
      // Default: an upstream that emits one frame and ends immediately.
      const u = makeControllableUpstream();
      setImmediate(() => {
        u.emitFrame("event: text\ndata: hi\n\n");
        u.finish();
      });
      return u.stream as any;
    },
    ...overrides,
  };
}

const servers: http.Server[] = [];

function mount(deps: InvokeHandlerDeps): Promise<string> {
  const app = express();
  app.post("/invoke/:agentId", createInvokeHandler(deps));
  return listenLoopback(app, servers);
}

const post = (base: string, agentId = "agent-xyz", body: unknown = { prompt: "hi" }) =>
  fetch(`${base}/invoke/${agentId}`, {
    method: "POST",
    headers: { authorization: "Bearer good.jwt.token", "content-type": "application/json" },
    body: JSON.stringify(body),
  });

afterEach(async () => {
  await closeServers(servers);
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// 15.4 — concurrent-stream cap → 503
// ---------------------------------------------------------------------------

describe("POST /invoke/:agentId — concurrent-stream cap → 503 (Req 15.4)", () => {
  it("holds the only slot for an in-flight stream, rejects a concurrent request with 503, then re-admits after release", async () => {
    const guard = createStreamGuard(1);

    // Hand out controllable upstreams so we can keep stream #1 open across the
    // concurrent second request, then close it to free the slot.
    const upstreams: ReturnType<typeof makeControllableUpstream>[] = [];
    const signedRequestStream = async () => {
      const u = makeControllableUpstream();
      upstreams.push(u);
      return u.stream as any;
    };

    const base = await mount(makeDeps({ streamGuard: guard, signedRequestStream }));

    // Request #1 — begins streaming and HOLDS the slot (upstream stays open).
    const res1 = await post(base, "agent-1");
    expect(res1.status).toBe(200);
    expect(res1.headers.get("content-type")).toBe("text/event-stream");

    // Wait until the handler has actually dispatched + reserved the slot.
    await vi.waitFor(() => {
      expect(upstreams.length).toBe(1);
      expect(guard.active).toBe(1);
    });

    // Request #2 — concurrent, while the slot is still held → 503.
    const res2 = await post(base, "agent-2");
    expect(res2.status).toBe(503);
    expect(res2.headers.get("retry-after")).toBe("1");
    const body2 = await res2.json();
    expect(body2.error.category).toBe("service_unavailable");

    // The rejected request must NOT have dispatched (still only one upstream).
    expect(upstreams.length).toBe(1);

    // End request #1's upstream → the slot is released.
    upstreams[0].emitFrame("event: text\ndata: done\n\n");
    upstreams[0].finish();
    await res1.text(); // drain to completion
    await vi.waitFor(() => expect(guard.active).toBe(0));

    // Request #3 — after release, a fresh request is admitted again.
    const res3 = await post(base, "agent-3");
    expect(res3.status).toBe(200);
    await vi.waitFor(() => expect(upstreams.length).toBe(2));
    upstreams[1].finish();
    await res3.text();
  });
});

// ---------------------------------------------------------------------------
// 15.2 — buffering disabled
// ---------------------------------------------------------------------------

describe("POST /invoke/:agentId — response buffering disabled (Req 15.2)", () => {
  it("sets x-accel-buffering: no and text/event-stream on the streaming response", async () => {
    const base = await mount(makeDeps());
    const res = await post(base);

    expect(res.status).toBe(200);
    expect(res.headers.get("x-accel-buffering")).toBe("no");
    expect(res.headers.get("content-type")).toBe("text/event-stream");

    // The flushHeaders path executed without error (stream drains cleanly).
    const text = await res.text();
    expect(text).toContain("event: text");
  });
});

// ---------------------------------------------------------------------------
// 15.1 — keepalive comment on an idle open stream
// ---------------------------------------------------------------------------

describe("POST /invoke/:agentId — idle keepalive frame (Req 15.1)", () => {
  it("writes a ': keep-alive' comment frame while the stream is idle, before any real frame", async () => {
    // Small keepalive so the idle heartbeat fires quickly; keep the ordering
    // invariant intact (keepalive < proxyIdle ≤ edgeIdle, runtime ≤ proxyIdle).
    const transport: TransportConfig = {
      ...DEFAULT_TRANSPORT_CONFIG,
      keepaliveMs: 40,
      proxyIdleTimeoutMs: 5_000,
      edgeIdleTimeoutMs: 5_000,
      runtimeStreamTimeoutMs: 5_000,
    };

    // An upstream that stays idle/open (emits nothing) long enough for a
    // keepalive to fire, then closes.
    const u = makeControllableUpstream();
    const base = await mount(
      makeDeps({
        transport,
        signedRequestStream: async () => u.stream as any,
      }),
    );

    const res = await post(base);
    expect(res.status).toBe(200);

    // Read the raw stream and look for the keepalive comment before any real
    // frame arrives. We stream chunks rather than awaiting text() so we can
    // close the upstream once we've observed the heartbeat.
    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    let seen = "";
    let sawKeepaliveFirst = false;

    // Give the idle heartbeat a few intervals to fire.
    const deadline = Date.now() + 2_000;
    while (Date.now() < deadline) {
      const { value, done } = await reader.read();
      if (done) break;
      seen += decoder.decode(value, { stream: true });
      if (seen.includes(": keep-alive")) {
        // No real frame should have preceded the first keepalive on an idle stream.
        sawKeepaliveFirst = !seen.includes("event: text");
        break;
      }
    }

    // Close the upstream and drain.
    u.emitFrame("event: text\ndata: finally\n\n");
    u.finish();
    try {
      // Drain any remaining bytes so the server can close the connection.
      // (Best effort — reader may already be at end.)
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done } = await reader.read();
        if (done) break;
      }
    } catch {
      /* ignore */
    }

    expect(seen).toContain(": keep-alive");
    expect(sawKeepaliveFirst).toBe(true);
  });
});
