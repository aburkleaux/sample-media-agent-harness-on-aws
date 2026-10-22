// Example / integration tests for the external invoke audit log + kill switch
// (task 17.2, Requirements 17.1 / 17.2 / 17.3 / 17.4).
//
// Two complementary layers:
//
//   1. Direct unit tests of the pure/stateful building blocks — `createKillSwitch`
//      (admin toggles, global-before-agent precedence) and `buildAuditEntry` /
//      `dispositionForStatus` (secret-free entry shape, status→disposition map).
//
//   2. Integration tests that mount the REAL `createInvokeHandler` (the exact
//      factory `ui/server/app.ts` registers at `POST /invoke/:agentId`) on a
//      throwaway Express app + HTTP server and drive it over the wire with
//      `fetch`, injecting a capturing `auditLogger` and a real `killSwitch`.
//      AWS-touching deps are fakes so the tests are deterministic and hit no
//      network/AWS.
//
// Asserted behaviour:
//   * Req 17.1 — every invocation (success AND denial) writes exactly ONE audit
//     entry carrying agentId, resolved published version, Owner, Caller `sub`,
//     and the outcome.
//   * Req 17.2 — the per-agent kill switch rejects that agent (503) only.
//   * Req 17.3 — the global kill switch rejects ALL agents (503).
//   * Req 17.4 — admin `setGlobal` / `setAgent` toggles work at runtime and are
//     independent of the Owner's `integration_config` / row state.
//
// Validates: Requirements 17.1, 17.2, 17.3, 17.4

import { describe, it, expect, afterEach, vi } from "vitest";
import { closeServers, listenLoopback } from "../testing/loopback";
import express from "express";
import http from "http";
import { PassThrough } from "stream";

import { createInvokeHandler, type InvokeHandlerDeps } from "./invokeRoute";
import { createKillSwitch } from "./killSwitch";
import {
  buildAuditEntry,
  dispositionForStatus,
  type AuditEntry,
  type AuditLogger,
} from "./auditLog";

// ---------------------------------------------------------------------------
// Fixtures & helpers
// ---------------------------------------------------------------------------

const TASK_URL =
  "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/task-arn/invocations?qualifier=DEFAULT";

const ISSUER = "https://issuer.example.com";
const AUDIENCE = "urn:my-agent";
const OWNER = "owner-user-123";
const CALLER_SUB = "caller-sub-789";
const AGENT_VERSION = 4;

/** A resolved current-published AssetIndex row with external integration on. */
function makeAgentRow(overrides: Record<string, any> = {}): Record<string, any> {
  return {
    ownerUserId: OWNER,
    version: AGENT_VERSION,
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

/** An in-memory audit sink that captures every recorded entry. */
function makeCapturingAuditLogger(): AuditLogger & { entries: AuditEntry[] } {
  const entries: AuditEntry[] = [];
  return {
    entries,
    record(entry: AuditEntry): void {
      entries.push(entry);
    },
  };
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

function invoke(base: string, agentId = "agent-xyz"): Promise<Response> {
  return fetch(`${base}/invoke/${agentId}`, {
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
// Unit layer — kill switch admin controls & precedence (Req 17.2/17.3/17.4)
// ---------------------------------------------------------------------------

describe("createKillSwitch — admin controls & precedence", () => {
  it("per-agent switch blocks only the named agent (Req 17.2)", () => {
    const ks = createKillSwitch({ agents: ["agent-a"] });

    const blocked = ks.evaluate("agent-a");
    expect(blocked).toEqual({ blocked: true, scope: "agent" });

    // A different agent is untouched.
    expect(ks.evaluate("agent-b")).toEqual({ blocked: false });
  });

  it("global switch blocks every agent and supersedes per-agent state (Req 17.3)", () => {
    const ks = createKillSwitch({ global: true, agents: ["agent-a"] });

    // Any agent id is blocked, and global is reported even for an agent that
    // is ALSO individually disabled (global is checked first).
    expect(ks.evaluate("agent-a")).toEqual({ blocked: true, scope: "global" });
    expect(ks.evaluate("literally-anything")).toEqual({
      blocked: true,
      scope: "global",
    });
  });

  it("admin setGlobal / setAgent toggle at runtime, independently (Req 17.4)", () => {
    const ks = createKillSwitch();

    // Nothing blocked initially.
    expect(ks.evaluate("agent-a").blocked).toBe(false);

    // Admin disables one agent — only that agent is affected.
    ks.setAgent("agent-a", true);
    expect(ks.isAgentDisabled("agent-a")).toBe(true);
    expect(ks.evaluate("agent-a")).toEqual({ blocked: true, scope: "agent" });
    expect(ks.evaluate("agent-b").blocked).toBe(false);

    // Admin flips the global switch — now everything is blocked, and the
    // per-agent state is independent (still set, just superseded).
    ks.setGlobal(true);
    expect(ks.isGloballyDisabled()).toBe(true);
    expect(ks.evaluate("agent-b")).toEqual({ blocked: true, scope: "global" });

    // Admin clears global — per-agent state for agent-a remains in effect.
    ks.setGlobal(false);
    expect(ks.isGloballyDisabled()).toBe(false);
    expect(ks.evaluate("agent-a")).toEqual({ blocked: true, scope: "agent" });
    expect(ks.evaluate("agent-b").blocked).toBe(false);

    // Admin re-enables agent-a — clean slate.
    ks.setAgent("agent-a", false);
    expect(ks.isAgentDisabled("agent-a")).toBe(false);
    expect(ks.evaluate("agent-a").blocked).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Unit layer — audit entry shape (Req 17.1) + status→disposition map
// ---------------------------------------------------------------------------

describe("buildAuditEntry / dispositionForStatus", () => {
  it("builds a secret-free entry with all identity/version/outcome fields (Req 17.1)", () => {
    const entry = buildAuditEntry(
      {
        agentId: "agent-xyz",
        publishedVersion: AGENT_VERSION,
        owner: OWNER,
        callerSub: CALLER_SUB,
        issuer: ISSUER,
      },
      { status: 200, category: "success" },
      () => new Date("2025-01-01T00:00:00.000Z"),
    );

    expect(entry).toEqual({
      timestamp: "2025-01-01T00:00:00.000Z",
      event: "external_invoke",
      agentId: "agent-xyz",
      publishedVersion: AGENT_VERSION,
      owner: OWNER,
      callerSub: CALLER_SUB,
      issuer: ISSUER,
      outcome: { disposition: "allowed", status: 200, category: "success" },
    });

    // No secret-bearing fields ever leak into the entry shape.
    expect(Object.keys(entry)).not.toContain("token");
    expect(Object.keys(entry)).not.toContain("authorization");
  });

  it("defaults unknown context fields to null (Req 17.1)", () => {
    const entry = buildAuditEntry(
      { agentId: "agent-xyz" },
      { status: 503, category: "kill_switch_global" },
    );
    expect(entry.publishedVersion).toBeNull();
    expect(entry.owner).toBeNull();
    expect(entry.callerSub).toBeNull();
    expect(entry.issuer).toBeNull();
    // A kill-switch stop returns HTTP 503, which is a 5xx → `error` disposition
    // per `dispositionForStatus`. The denial is still fully traceable via the
    // status (503) and the fine-grained category (`kill_switch_global`).
    expect(entry.outcome).toEqual({
      disposition: "error",
      status: 503,
      category: "kill_switch_global",
    });
  });

  it("maps status classes to dispositions (2xx allowed / 4xx denied / 5xx error)", () => {
    expect(dispositionForStatus(200)).toBe("allowed");
    expect(dispositionForStatus(401)).toBe("denied");
    expect(dispositionForStatus(404)).toBe("denied");
    expect(dispositionForStatus(429)).toBe("denied");
    // 5xx (including the 503 emergency-stop) classes as `error`, not `denied`.
    expect(dispositionForStatus(503)).toBe("error");
    expect(dispositionForStatus(500)).toBe("error");
    expect(dispositionForStatus(502)).toBe("error");
  });
});

// ---------------------------------------------------------------------------
// Integration layer — audit entries via the real handler (Req 17.1)
// ---------------------------------------------------------------------------

describe("POST /invoke/:agentId — audit logging (Req 17.1)", () => {
  it("writes exactly one audit entry with full context on a successful invocation", async () => {
    const audit = makeCapturingAuditLogger();
    const base = await mount(makeDeps({ auditLogger: audit }));

    const res = await invoke(base, "agent-xyz");
    expect(res.status).toBe(200);
    await res.text(); // drain the SSE stream so the `end` audit fires

    expect(audit.entries).toHaveLength(1);
    const entry = audit.entries[0];
    expect(entry.event).toBe("external_invoke");
    expect(entry.agentId).toBe("agent-xyz");
    expect(entry.publishedVersion).toBe(AGENT_VERSION);
    expect(entry.owner).toBe(OWNER);
    expect(entry.callerSub).toBe(CALLER_SUB);
    expect(entry.issuer).toBe(ISSUER);
    expect(entry.outcome).toEqual({
      disposition: "allowed",
      status: 200,
      category: "success",
    });
  });

  it("writes exactly one audit entry for a denial (401 missing token) too", async () => {
    const audit = makeCapturingAuditLogger();
    const base = await mount(makeDeps({ auditLogger: audit }));

    // No Authorization header → 401 missing_token, still audited.
    const res = await fetch(`${base}/invoke/agent-xyz`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ prompt: "hello" }),
    });
    expect(res.status).toBe(401);
    await res.json();

    expect(audit.entries).toHaveLength(1);
    const entry = audit.entries[0];
    expect(entry.agentId).toBe("agent-xyz");
    // Owner/version were resolved before the token check, so they're recorded.
    expect(entry.owner).toBe(OWNER);
    expect(entry.publishedVersion).toBe(AGENT_VERSION);
    // Caller sub is unknown (token never verified) → null.
    expect(entry.callerSub).toBeNull();
    expect(entry.outcome).toEqual({
      disposition: "denied",
      status: 401,
      category: "missing_token",
    });
  });

  it("audits a 404 (integration disabled) denial with resolved owner/version", async () => {
    const audit = makeCapturingAuditLogger();
    const base = await mount(
      makeDeps({
        auditLogger: audit,
        resolveCurrentPublishedAgent: async () =>
          makeAgentRow({ integrationConfig: { enabled: false } }),
      }),
    );

    const res = await invoke(base, "agent-xyz");
    expect(res.status).toBe(404);
    await res.json();

    expect(audit.entries).toHaveLength(1);
    expect(audit.entries[0].outcome).toEqual({
      disposition: "denied",
      status: 404,
      category: "not_found",
    });
    expect(audit.entries[0].owner).toBe(OWNER);
  });
});

// ---------------------------------------------------------------------------
// Integration layer — kill switch via the real handler (Req 17.2/17.3/17.4)
// ---------------------------------------------------------------------------

describe("POST /invoke/:agentId — kill switch (Req 17.2/17.3/17.4)", () => {
  it("per-agent kill switch rejects that agent with 503 (and audits it) — Req 17.2", async () => {
    const audit = makeCapturingAuditLogger();
    const killSwitch = createKillSwitch({ agents: ["agent-blocked"] });
    let dispatched = 0;
    const base = await mount(
      makeDeps({
        killSwitch,
        auditLogger: audit,
        signedRequestStream: async () => {
          dispatched += 1;
          return makeUpstream() as any;
        },
      }),
    );

    // Blocked agent → 503 with Retry-After, never dispatched.
    const blocked = await invoke(base, "agent-blocked");
    expect(blocked.status).toBe(503);
    expect(blocked.headers.get("retry-after")).toBe("60");
    const body = await blocked.json();
    expect(body.error.category).toBe("service_unavailable");
    expect(dispatched).toBe(0);

    // Audit records the denial with the kill-switch category.
    expect(audit.entries).toHaveLength(1);
    expect(audit.entries[0].agentId).toBe("agent-blocked");
    // 503 emergency-stop → `error` disposition; the kill-switch category makes
    // the denial reason explicit and queryable.
    expect(audit.entries[0].outcome).toEqual({
      disposition: "error",
      status: 503,
      category: "kill_switch_agent",
    });

    // A different agent is unaffected and still streams.
    const ok = await invoke(base, "agent-open");
    expect(ok.status).toBe(200);
    await ok.text();
    expect(dispatched).toBe(1);
  });

  it("global kill switch rejects ALL agents with 503 — Req 17.3", async () => {
    const audit = makeCapturingAuditLogger();
    const killSwitch = createKillSwitch({ global: true });
    let dispatched = 0;
    const base = await mount(
      makeDeps({
        killSwitch,
        auditLogger: audit,
        signedRequestStream: async () => {
          dispatched += 1;
          return makeUpstream() as any;
        },
      }),
    );

    for (const agentId of ["agent-a", "agent-b", "another-one"]) {
      const res = await invoke(base, agentId);
      expect(res.status).toBe(503);
      expect(res.headers.get("retry-after")).toBe("60");
      const body = await res.json();
      expect(body.error.category).toBe("service_unavailable");
    }
    expect(dispatched).toBe(0);

    // Each rejection audited as a global kill-switch denial.
    expect(audit.entries).toHaveLength(3);
    for (const entry of audit.entries) {
      expect(entry.outcome.category).toBe("kill_switch_global");
      expect(entry.outcome.status).toBe(503);
      expect(entry.outcome.disposition).toBe("error");
    }
  });

  it("admin toggle takes effect at runtime and is independent of Owner config — Req 17.4", async () => {
    const audit = makeCapturingAuditLogger();
    // The killSwitch starts empty; the Owner's integration_config stays enabled
    // throughout, so any rejection is driven by the ADMIN switch, not the Owner.
    const killSwitch = createKillSwitch();
    let dispatched = 0;
    const base = await mount(
      makeDeps({
        killSwitch,
        auditLogger: audit,
        resolveCurrentPublishedAgent: async () => makeAgentRow(), // always enabled
        signedRequestStream: async () => {
          dispatched += 1;
          return makeUpstream() as any;
        },
      }),
    );

    // 1) Before any admin action: invocation succeeds (Owner config enabled).
    const before = await invoke(base, "agent-xyz");
    expect(before.status).toBe(200);
    await before.text();
    expect(dispatched).toBe(1);

    // 2) Admin flips the per-agent switch — no change to the Owner's row/config.
    killSwitch.setAgent("agent-xyz", true);
    const afterAgent = await invoke(base, "agent-xyz");
    expect(afterAgent.status).toBe(503);
    await afterAgent.json();
    expect(dispatched).toBe(1); // still not dispatched

    // 3) Admin clears the per-agent switch and flips global instead.
    killSwitch.setAgent("agent-xyz", false);
    killSwitch.setGlobal(true);
    const afterGlobal = await invoke(base, "agent-xyz");
    expect(afterGlobal.status).toBe(503);
    await afterGlobal.json();
    expect(dispatched).toBe(1);

    // 4) Admin clears global — invocation flows again with the SAME Owner config.
    killSwitch.setGlobal(false);
    const restored = await invoke(base, "agent-xyz");
    expect(restored.status).toBe(200);
    await restored.text();
    expect(dispatched).toBe(2);
  });
});
