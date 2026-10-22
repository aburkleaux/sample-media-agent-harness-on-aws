// Example test for immediate external-access disable (task 16.3).
//
// Exercises the request-time effective-access gate (Req 16.1): when the Owner
// disables external access, subsequent external invocations are rejected
// PROMPTLY and INDEPENDENTLY of any version publish. The proxy resolves
// effective access = `snapshot.integration_config.enabled AND
// external_access_enabled` at REQUEST TIME, so flipping the row's
// `externalAccessEnabled` gate to `false` takes effect on the very next
// request — no re-publish, no new snapshot. Revocation folds into the same
// `not_found` (404) taxonomy category as an integration-disabled agent so it
// does not leak whether the agent exists.
//
// These mount the REAL route handler produced by `createInvokeHandler` (the
// exact factory `ui/server/app.ts` registers at `app.post("/invoke/:agentId")`)
// on a throwaway Express app + HTTP server and drive it over the wire with
// `fetch`. AWS-touching deps are injected as fakes so the test is deterministic
// and hits no network/AWS.
//
// Validates: Requirements 16.1

import { describe, it, expect, afterEach, vi } from "vitest";
import { closeServers, listenLoopback } from "../testing/loopback";
import express from "express";
import http from "http";
import { PassThrough } from "stream";

import {
  createInvokeHandler,
  type InvokeHandlerDeps,
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

/**
 * A resolved current-published AssetIndex row with external integration on.
 * The immutable snapshot's `integrationConfig.enabled` is TRUE; the mutable
 * request-time revocation gate lives in `externalAccessEnabled` on the row,
 * SEPARATE from the snapshot. Absent → treated as enabled; only an explicit
 * `false` revokes.
 */
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

/** A fake upstream `IncomingMessage`: emits one SSE frame then ends cleanly. */
function makeUpstream(): PassThrough {
  const pt = new PassThrough() as PassThrough & {
    statusCode?: number;
    headers?: Record<string, string>;
  };
  pt.statusCode = 200;
  pt.headers = { "content-type": "text/event-stream" };
  setImmediate(() => {
    pt.write("event: text\ndata: hi\n\n");
    pt.end();
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
    signedRequestStream: async () => makeUpstream() as any,
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

function invoke(base: string): Promise<Response> {
  return fetch(`${base}/invoke/agent-xyz`, {
    method: "POST",
    headers: {
      authorization: "Bearer good.jwt.token",
      "content-type": "application/json",
    },
    body: JSON.stringify({ prompt: "hello" }),
  });
}

afterEach(async () => {
  await closeServers(servers);
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// 16.1 — immediate disable, resolved at request time, no publish required
// ---------------------------------------------------------------------------

describe("POST /invoke/:agentId — immediate external-access disable (Req 16.1)", () => {
  it("rejects with 404 when external access is revoked even though the published snapshot stays enabled", async () => {
    let dispatched = 0;
    const base = await mount(
      makeDeps({
        // Same current published snapshot (integration enabled), but the row's
        // request-time revocation gate is flipped OFF. No re-publish occurred.
        resolveCurrentPublishedAgent: async () =>
          makeAgentRow({ externalAccessEnabled: false }),
        signedRequestStream: async () => {
          dispatched += 1;
          return makeUpstream() as any;
        },
      }),
    );

    const res = await invoke(base);

    // Rejected via the not_found (404) taxonomy — no leak of existence.
    expect(res.status).toBe(404);
    const body = await res.json();
    expect(body.error.category).toBe("not_found");
    // Snapshot integration is still enabled: rejection came from the gate, not
    // a disabled snapshot — and dispatch was never reached.
    expect(dispatched).toBe(0);
  });

  it("takes effect on the very next request after disable, without a new publish", async () => {
    // A single mutable row read fresh on EACH request models request-time
    // resolution: the published snapshot never changes (enabled: true), only
    // the Owner's revocation gate is toggled between the two invocations.
    const row = makeAgentRow(); // externalAccessEnabled absent → enabled
    let dispatched = 0;

    const base = await mount(
      makeDeps({
        resolveCurrentPublishedAgent: async () => ({ ...row }),
        signedRequestStream: async () => {
          dispatched += 1;
          return makeUpstream() as any;
        },
      }),
    );

    // 1) Before disable: invocation succeeds and streams SSE.
    const before = await invoke(base);
    expect(before.status).toBe(200);
    expect(before.headers.get("content-type")).toBe("text/event-stream");
    await before.text();
    expect(dispatched).toBe(1);

    // Owner disables external access — no publish, snapshot config untouched.
    row.externalAccessEnabled = false;
    expect(row.integrationConfig.enabled).toBe(true); // snapshot still enabled

    // 2) After disable: the very next request is rejected 404, no new dispatch.
    const after = await invoke(base);
    expect(after.status).toBe(404);
    const body = await after.json();
    expect(body.error.category).toBe("not_found");
    expect(dispatched).toBe(1);
  });

  it("still serves invocations while the gate is enabled or absent (only explicit false revokes)", async () => {
    // Sanity guard on the gate semantics: an absent attribute (legacy rows) and
    // an explicit `true` both leave access ENABLED; only `false` revokes.
    for (const gate of [undefined, true]) {
      const base = await mount(
        makeDeps({
          resolveCurrentPublishedAgent: async () =>
            makeAgentRow(
              gate === undefined ? {} : { externalAccessEnabled: gate },
            ),
        }),
      );
      const res = await invoke(base);
      expect(res.status).toBe(200);
      expect(res.headers.get("content-type")).toBe("text/event-stream");
      await res.text();
    }
  });
});
