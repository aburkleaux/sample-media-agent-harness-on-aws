// Integration test for the full ordered external-invoke pipeline
// (POST /invoke/:agentId) — task 19.2, Requirements 5.7 / 14.2 / 16.1 / 17.2.
//
// This mounts the REAL handler produced by `createInvokeHandler` — the exact
// factory `ui/server/app.ts` registers at `app.post("/invoke/:agentId")` — with
// ALL gates wired at once (killSwitch + rateLimiter + auditLogger + streamGuard
// + transport + fake resolver/verifier/signer) on a throwaway Express app +
// ephemeral HTTP server, and drives it over the wire with `fetch`. The
// AWS-touching deps are injected as deterministic fakes so the test hits no
// network/AWS.
//
// It pins the FIXED middleware order (design §Component 4) end-to-end:
//
//   0. kill-switch gate (Req 17)                       → 503
//   1. per-agent rate-limit peek (Req 14, NON-consuming) → 429
//   2. resolve + effective external-access gate (Req 16) → 404
//   3. Bearer token extract (missing)                  → 401 missing_token
//   4. SSRF-hardened OIDC verify (Req 12/13)           → 401 (verifier categories)
//   5. client-id gate (Req 5.6)                        → 403 client_not_allowed
//   6. subject required                                → 401 missing_subject
//   7. authoritative per-caller rate-limit consume     → 429
//   8. two-identity stamp + EXT# session + SigV4 dispatch + SSE stream + audit
//
// Asserted here:
//   * Each gate SHORT-CIRCUITS with its correct HTTP status and dispatch
//     (`signedRequestStream`) is NEVER reached on any short-circuit (Req 5.7 —
//     no invocation happens unless every gate passes).
//   * ORDERING — when two gates would both fire, the EARLIER one wins
//     (kill-switch beats a missing token; resolution/revocation beats a missing
//     token; per-agent peek beats resolution).
//   * A fully valid request dispatches EXACTLY ONCE and streams SSE end-to-end
//     (content-type text/event-stream, body carries the upstream frame) with a
//     single audit `success` entry.
//
// Validates: Requirements 5.7, 14.2, 16.1, 17.2

import { describe, it, expect, afterEach, vi } from "vitest";
import { closeServers, listenLoopback } from "../testing/loopback";
import express from "express";
import http from "http";
import { PassThrough } from "stream";

import { createInvokeHandler, type InvokeHandlerDeps } from "./invokeRoute";
import { createKillSwitch } from "./killSwitch";
import { createStreamGuard } from "./streamGuard";
import {
  InvokeRateLimiter,
  type RateLimiterOptions,
} from "./rateLimiter";
import { AgentTokenError } from "../auth/agentOidc";
import type { AuditEntry, AuditLogger } from "./auditLog";

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
const UPSTREAM_FRAME = "event: text\ndata: hi\n\n";

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
    pt.write(UPSTREAM_FRAME);
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

/**
 * A dispatch spy: counts calls and returns a fresh happy upstream each time.
 * All short-circuit assertions check `dispatch.calls === 0` — i.e. the pipeline
 * NEVER reached SigV4 dispatch (Req 5.7). Also captures the outbound body +
 * headers so the proxy→runtime payload CONTRACT can be asserted (the
 * custom-agents seam: identity + published-version reference, no config).
 */
function makeDispatchSpy() {
  const spy: { calls: number; bodies: any[]; headers: Record<string, string>[] } = {
    calls: 0,
    bodies: [],
    headers: [],
  };
  const fn: InvokeHandlerDeps["signedRequestStream"] = async (
    _url: string,
    body?: any,
    headers?: Record<string, string>,
  ) => {
    spy.calls += 1;
    if (body != null) {
      try {
        spy.bodies.push(JSON.parse(Buffer.isBuffer(body) ? body.toString("utf8") : String(body)));
      } catch {
        spy.bodies.push(body);
      }
    }
    spy.headers.push(headers ?? {});
    return makeUpstream() as any;
  };
  return { spy, fn };
}

/**
 * Generous rate-limiter options that admit the happy path (large burst + fast
 * refill) — individual ordering tests still exercise real rejection by driving
 * a bespoke limiter with a tiny capacity.
 */
function generousRateLimiterOptions(): RateLimiterOptions {
  return {
    perAgent: { burst: { capacity: 100, refillPerSec: 100 } },
    perCaller: { burst: { capacity: 100, refillPerSec: 100 } },
  };
}

/**
 * Build deps with EVERY gate wired: a real killSwitch, a real rateLimiter, a
 * real streamGuard, a capturing auditLogger, plus fake resolver/verifier/signer.
 * Individual tests override just the pieces they exercise.
 */
function makeFullDeps(overrides: Partial<InvokeHandlerDeps> = {}): {
  deps: InvokeHandlerDeps;
  audit: AuditLogger & { entries: AuditEntry[] };
  dispatch: { calls: number; bodies: any[]; headers: Record<string, string>[] };
} {
  const audit = makeCapturingAuditLogger();
  const { spy, fn } = makeDispatchSpy();
  const deps: InvokeHandlerDeps = {
    invokeTaskUrl: TASK_URL,
    assetIndexReady: true,
    resolveCurrentPublishedAgent: async () => makeAgentRow(),
    verifyAgentToken: async () => ({ sub: CALLER_SUB, clientId: null }),
    signedRequestStream: fn,
    killSwitch: createKillSwitch(),
    rateLimiter: new InvokeRateLimiter(generousRateLimiterOptions()),
    streamGuard: createStreamGuard(50),
    auditLogger: audit,
    ...overrides,
  };
  return { deps, audit, dispatch: spy };
}

const servers: http.Server[] = [];

/** Mount the real handler on a fresh Express app + ephemeral HTTP server. */
function mount(deps: InvokeHandlerDeps): Promise<string> {
  const app = express();
  app.post("/invoke/:agentId", createInvokeHandler(deps));
  return listenLoopback(app, servers);
}

/** POST with a valid Bearer token by default; opts can strip/replace headers. */
function invoke(
  base: string,
  opts: { agentId?: string; authorization?: string | null } = {},
): Promise<Response> {
  const agentId = opts.agentId ?? "agent-xyz";
  const headers: Record<string, string> = { "content-type": "application/json" };
  const auth = opts.authorization === undefined ? "Bearer good.jwt.token" : opts.authorization;
  if (auth != null) headers.authorization = auth;
  return fetch(`${base}/invoke/${agentId}`, {
    method: "POST",
    headers,
    body: JSON.stringify({ prompt: "hello" }),
  });
}

afterEach(async () => {
  await closeServers(servers);
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// 1. Each gate short-circuits with its correct status — dispatch NEVER reached
// ---------------------------------------------------------------------------

describe("POST /invoke/:agentId — each gate short-circuits before dispatch (Req 5.7)", () => {
  it("gate 0: active kill switch → 503, no dispatch", async () => {
    const { deps, audit, dispatch } = makeFullDeps({
      killSwitch: createKillSwitch({ agents: ["agent-xyz"] }),
    });
    const base = await mount(deps);

    const res = await invoke(base);
    expect(res.status).toBe(503);
    expect(res.headers.get("retry-after")).toBe("60");
    const body = await res.json();
    expect(body.error.category).toBe("service_unavailable");
    expect(dispatch.calls).toBe(0);
    // Audited as a kill-switch denial.
    expect(audit.entries).toHaveLength(1);
    expect(audit.entries[0].outcome.category).toBe("kill_switch_agent");
  });

  it("gate 1: per-agent rate-limit peek exceeded → 429, no dispatch", async () => {
    // A limiter whose per-agent bucket is empty (capacity 0, no refill) rejects
    // on the NON-consuming peek at step 2 — before resolve/verify ever run.
    const rateLimiter = new InvokeRateLimiter({
      perAgent: { burst: { capacity: 0, refillPerSec: 0, windowFallbackSec: 30 } },
      perCaller: { burst: { capacity: 100, refillPerSec: 100 } },
    });
    let resolved = 0;
    const { deps, dispatch } = makeFullDeps({
      rateLimiter,
      resolveCurrentPublishedAgent: async () => {
        resolved += 1;
        return makeAgentRow();
      },
    });
    const base = await mount(deps);

    const res = await invoke(base);
    expect(res.status).toBe(429);
    expect(res.headers.get("retry-after")).toBe("30");
    const body = await res.json();
    expect(body.error.category).toBe("rate_limited");
    expect(dispatch.calls).toBe(0);
    // The cheap per-agent peek shed load BEFORE the expensive resolve ran.
    expect(resolved).toBe(0);
  });

  it("gate 2: external access revoked → 404, no dispatch (Req 16.1)", async () => {
    const { deps, dispatch } = makeFullDeps({
      resolveCurrentPublishedAgent: async () =>
        makeAgentRow({ externalAccessEnabled: false }),
    });
    const base = await mount(deps);

    const res = await invoke(base);
    expect(res.status).toBe(404);
    const body = await res.json();
    expect(body.error.category).toBe("not_found");
    expect(dispatch.calls).toBe(0);
  });

  it("gate 2: integration disabled → 404, no dispatch", async () => {
    const { deps, dispatch } = makeFullDeps({
      resolveCurrentPublishedAgent: async () =>
        makeAgentRow({ integrationConfig: { enabled: false } }),
    });
    const base = await mount(deps);

    const res = await invoke(base);
    expect(res.status).toBe(404);
    expect((await res.json()).error.category).toBe("not_found");
    expect(dispatch.calls).toBe(0);
  });

  it("gate 3: missing token → 401 missing_token, no dispatch", async () => {
    const { deps, dispatch } = makeFullDeps();
    const base = await mount(deps);

    const res = await invoke(base, { authorization: null });
    expect(res.status).toBe(401);
    expect(res.headers.get("www-authenticate")).toBe('Bearer error="missing_token"');
    expect((await res.json()).error.category).toBe("missing_token");
    expect(dispatch.calls).toBe(0);
  });

  it("gate 4: invalid token → 401 invalid_token, no dispatch", async () => {
    const { deps, dispatch } = makeFullDeps({
      verifyAgentToken: async () => {
        throw new AgentTokenError("invalid_token", "bad signature");
      },
    });
    const base = await mount(deps);

    const res = await invoke(base);
    expect(res.status).toBe(401);
    expect(res.headers.get("www-authenticate")).toBe('Bearer error="invalid_token"');
    expect((await res.json()).error.category).toBe("invalid_token");
    expect(dispatch.calls).toBe(0);
  });

  it("gate 5: client_id not in allowlist → 403 client_not_allowed, no dispatch", async () => {
    const { deps, dispatch } = makeFullDeps({
      resolveCurrentPublishedAgent: async () =>
        makeAgentRow({
          integrationConfig: {
            enabled: true,
            issuer: ISSUER,
            audience: AUDIENCE,
            allowedClientIds: ["allowed-client"],
          },
        }),
      // Verified token carries a client_id NOT on the allowlist.
      verifyAgentToken: async () => ({ sub: CALLER_SUB, clientId: "other-client" }),
    });
    const base = await mount(deps);

    const res = await invoke(base);
    expect(res.status).toBe(403);
    expect((await res.json()).error.category).toBe("client_not_allowed");
    expect(dispatch.calls).toBe(0);
  });

  it("gate 6: missing subject → 401 missing_subject, no dispatch", async () => {
    const { deps, dispatch } = makeFullDeps({
      verifyAgentToken: async () => ({ sub: "", clientId: null }),
    });
    const base = await mount(deps);

    const res = await invoke(base);
    expect(res.status).toBe(401);
    expect(res.headers.get("www-authenticate")).toBe('Bearer error="missing_subject"');
    expect((await res.json()).error.category).toBe("missing_subject");
    expect(dispatch.calls).toBe(0);
  });

  it("gate 7: authoritative per-caller quota exceeded → 429, no dispatch (Req 14.2)", async () => {
    // Per-agent admits (peek passes), but the per-caller bucket is empty, so the
    // AUTHORITATIVE consume after verification rejects — still before dispatch.
    const rateLimiter = new InvokeRateLimiter({
      perAgent: { burst: { capacity: 100, refillPerSec: 100 } },
      perCaller: { burst: { capacity: 0, refillPerSec: 0, windowFallbackSec: 15 } },
    });
    const { deps, dispatch } = makeFullDeps({ rateLimiter });
    const base = await mount(deps);

    const res = await invoke(base);
    expect(res.status).toBe(429);
    expect(res.headers.get("retry-after")).toBe("15");
    const body = await res.json();
    expect(body.error.category).toBe("rate_limited");
    expect(body.error.message).toContain("per-caller");
    expect(dispatch.calls).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// 2. Ordering — when two gates would both fire, the EARLIER one wins
// ---------------------------------------------------------------------------

describe("POST /invoke/:agentId — fixed middleware ordering (design §Component 4)", () => {
  it("kill-switch (gate 0) beats a missing token (gate 3): both bad → 503", async () => {
    const { deps, dispatch } = makeFullDeps({
      killSwitch: createKillSwitch({ global: true }),
    });
    const base = await mount(deps);

    // No Authorization header AND the global kill switch is active. Kill switch
    // is checked FIRST, so the response is 503 (not 401).
    const res = await invoke(base, { authorization: null });
    expect(res.status).toBe(503);
    expect((await res.json()).error.category).toBe("service_unavailable");
    expect(dispatch.calls).toBe(0);
  });

  it("per-agent rate-limit (gate 1) beats resolution/404 (gate 2): both bad → 429", async () => {
    // The per-agent peek is empty (429) AND the agent would resolve as revoked
    // (404). The peek runs at step 2, BEFORE resolution, so 429 wins and the
    // resolver is never even consulted.
    const rateLimiter = new InvokeRateLimiter({
      perAgent: { burst: { capacity: 0, refillPerSec: 0, windowFallbackSec: 5 } },
      perCaller: { burst: { capacity: 100, refillPerSec: 100 } },
    });
    let resolved = 0;
    const { deps, dispatch } = makeFullDeps({
      rateLimiter,
      resolveCurrentPublishedAgent: async () => {
        resolved += 1;
        return makeAgentRow({ externalAccessEnabled: false });
      },
    });
    const base = await mount(deps);

    const res = await invoke(base, { authorization: null });
    expect(res.status).toBe(429);
    expect((await res.json()).error.category).toBe("rate_limited");
    expect(resolved).toBe(0);
    expect(dispatch.calls).toBe(0);
  });

  it("resolution/404 (gate 2) beats a missing token (gate 3): revoked + no token → 404", async () => {
    // External access revoked AND no Authorization header. Resolution + the
    // effective-access gate precede the token check, so 404 wins (not 401) —
    // and existence isn't leaked to an unauthenticated caller (Req 16.1).
    const { deps, dispatch } = makeFullDeps({
      resolveCurrentPublishedAgent: async () =>
        makeAgentRow({ externalAccessEnabled: false }),
    });
    const base = await mount(deps);

    const res = await invoke(base, { authorization: null });
    expect(res.status).toBe(404);
    expect((await res.json()).error.category).toBe("not_found");
    expect(dispatch.calls).toBe(0);
  });

  it("invalid token (gate 4) beats the client-id gate (gate 5): verify fails first → 401", async () => {
    // The allowlist is non-empty (so the client-id gate WOULD fire), but the
    // verifier throws first — verification precedes the client-id check.
    const { deps, dispatch } = makeFullDeps({
      resolveCurrentPublishedAgent: async () =>
        makeAgentRow({
          integrationConfig: {
            enabled: true,
            issuer: ISSUER,
            audience: AUDIENCE,
            allowedClientIds: ["allowed-client"],
          },
        }),
      verifyAgentToken: async () => {
        throw new AgentTokenError("expired", "token expired");
      },
    });
    const base = await mount(deps);

    const res = await invoke(base);
    expect(res.status).toBe(401);
    expect((await res.json()).error.category).toBe("expired");
    expect(dispatch.calls).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// 3. Fully valid request — dispatches once, streams SSE end-to-end, audits ok
// ---------------------------------------------------------------------------

describe("POST /invoke/:agentId — fully valid request streams end-to-end (Req 5.7)", () => {
  it("passes every gate, dispatches EXACTLY once, streams the upstream SSE frame, and audits success", async () => {
    const { deps, audit, dispatch } = makeFullDeps();
    const base = await mount(deps);

    const res = await invoke(base);

    // HTTP 200 SSE response with buffering disabled.
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("text/event-stream");
    expect(res.headers.get("x-accel-buffering")).toBe("no");

    // Body carries the upstream frame verbatim, streamed end-to-end.
    const text = await res.text();
    expect(text).toContain(UPSTREAM_FRAME.trim());

    // Dispatch happened EXACTLY once (every gate passed, charged once).
    expect(dispatch.calls).toBe(1);

    // Exactly one audit entry, recording a successful outcome with full context.
    expect(audit.entries).toHaveLength(1);
    const entry = audit.entries[0];
    expect(entry.agentId).toBe("agent-xyz");
    expect(entry.owner).toBe(OWNER);
    expect(entry.publishedVersion).toBe(AGENT_VERSION);
    expect(entry.callerSub).toBe(CALLER_SUB);
    expect(entry.issuer).toBe(ISSUER);
    expect(entry.outcome).toEqual({
      disposition: "allowed",
      status: 200,
      category: "success",
    });
  });
});

// ---------------------------------------------------------------------------
// 3. Outbound-body contract — the proxy→runtime seam (custom-agents seam fix)
// ---------------------------------------------------------------------------
//
// These assertions lock the EXACT payload shape the runtime's
// `task_agent._fetch_external_custom_agent_config` consumes (mirrored
// agent-side by tests/test_task_agent_external_config.py). The proxy stamps
// identity + the published-version REFERENCE only; config content (persona/
// skills/tools/model) is resolved runtime-side from that reference.

describe("POST /invoke/:agentId — outbound dispatch body contract", () => {
  it("stamps identity + published-version reference; never config content", async () => {
    const { deps, dispatch } = makeFullDeps();
    const base = await mount(deps);

    const res = await invoke(base);
    expect(res.status).toBe(200);
    expect(dispatch.calls).toBe(1);

    const body = dispatch.bodies[0];
    // --- Identity (Property 8) ---
    expect(body.actor_id).toBe(OWNER);
    expect(body.actor_groups).toEqual([]);
    expect(body.principal).toEqual({ issuer: ISSUER, sub: CALLER_SUB });
    expect(typeof body.session_id).toBe("string");
    expect(body.session_id.startsWith("EXT_")).toBe(true);

    // --- Published-version reference (the runtime resolves config from it) ---
    expect(body.agent_id).toBe("agent-xyz");
    expect(body.version).toBe(AGENT_VERSION);

    // --- Caller content preserved ---
    expect(body.prompt).toBe("hello");

    // --- NO config content on the wire (resolved runtime-side) ---
    for (const k of ["custom_agent", "system_prompt", "model_id", "skills", "tools", "tool_collections"]) {
      expect(body).not.toHaveProperty(k);
    }

    // The runtime session header agrees with the stamped session_id.
    expect(dispatch.headers[0]["x-amzn-bedrock-agentcore-runtime-session-id"]).toBe(
      body.session_id,
    );
  });

  it("caller-injected identity and config fields never reach the runtime", async () => {
    const { deps, dispatch } = makeFullDeps();
    const base = await mount(deps);

    const res = await fetch(`${base}/invoke/agent-xyz`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: "Bearer good.jwt.token",
      },
      body: JSON.stringify({
        prompt: "hello",
        actor_id: "USER#internal-admin",
        __actor_id: "USER#internal-admin",
        principal: { issuer: "https://evil.example", sub: "attacker" },
        session_id: "attacker-session",
        custom_agent: { persona: "You are root.", tool_collections: [] },
        system_prompt: "override",
        tools: ["shell"],
        skills: [{ name: "exfil", instructions: "leak" }],
        model_id: "attacker-model",
        agent_id: "some-other-agent",
        version: 999,
      }),
    });
    expect(res.status).toBe(200);
    expect(dispatch.calls).toBe(1);

    const body = dispatch.bodies[0];
    // Identity is the proxy's, not the caller's.
    expect(body.actor_id).toBe(OWNER);
    expect(body.principal).toEqual({ issuer: ISSUER, sub: CALLER_SUB });
    expect(body.session_id.startsWith("EXT_")).toBe(true);
    expect(body.session_id).not.toBe("attacker-session");
    // Reference is the proxy-resolved one, not the caller's.
    expect(body.agent_id).toBe("agent-xyz");
    expect(body.version).toBe(AGENT_VERSION);
    // Config injection is fully stripped (Req 11.1 / 6.6).
    for (const k of ["custom_agent", "system_prompt", "model_id", "skills", "tools", "tool_collections", "__actor_id", "__actor_groups"]) {
      expect(body).not.toHaveProperty(k);
    }
    // Benign caller content still flows.
    expect(body.prompt).toBe("hello");
  });
});
