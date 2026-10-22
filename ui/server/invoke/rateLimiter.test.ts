// Example/integration tests for the external-invoke rate limiter + quota gate
// and its wiring into the invoke route (task 14.2).
//
// Two layers:
//   1. UNIT — drive `InvokeRateLimiter` directly with a low burst capacity and
//      an INJECTED clock so time is deterministic (no real timers). Asserts:
//        * repeated check() admits up to the cap, then rejects with
//          allowed=false + scope/reason/retryAfterSec (14.1, 14.2)
//        * the per-agent scope rejects independently of per-caller and vice
//          versa (14.1)
//        * refill over injected time re-admits (14.1)
//   2. INTEGRATION — mount the REAL `createInvokeHandler` (task 9.4 harness
//      pattern: fake resolve/verify/signedRequestStream) with a limiter whose
//      per-caller capacity is 1, and assert an over-limit request returns HTTP
//      429 with a `Retry-After` header and NEVER dispatches (14.1, 14.2).
//
// Validates: Requirements 14.1, 14.2

import { describe, it, expect, afterEach, vi } from "vitest";
import { closeServers, listenLoopback } from "../testing/loopback";
import express from "express";
import http from "http";
import { PassThrough } from "stream";

import {
  InvokeRateLimiter,
  principalKey,
  type RateLimiterOptions,
} from "./rateLimiter";
import {
  createInvokeHandler,
  type InvokeHandlerDeps,
  type SignedRequestStreamFn,
} from "./invokeRoute";

// ---------------------------------------------------------------------------
// Layer 1 — unit tests with an injected clock
// ---------------------------------------------------------------------------

/** A controllable clock: tests advance `now` explicitly (epoch ms). */
function makeClock() {
  let now = 1_000_000;
  return {
    fn: () => now,
    advanceSec(sec: number) {
      now += sec * 1000;
    },
  };
}

/**
 * Build a limiter with small, independent burst caps and NO fixed-window quota
 * so the token bucket is the sole gate (keeps the arithmetic obvious).
 */
function makeLimiter(
  clock: () => number,
  opts: { agentCap: number; agentRefill: number; callerCap: number; callerRefill: number },
): InvokeRateLimiter {
  const options: RateLimiterOptions = {
    perAgent: { burst: { capacity: opts.agentCap, refillPerSec: opts.agentRefill } },
    perCaller: { burst: { capacity: opts.callerCap, refillPerSec: opts.callerRefill } },
    clock,
  };
  return new InvokeRateLimiter(options);
}

describe("InvokeRateLimiter — burst capacity + reject shape (Req 14.1, 14.2)", () => {
  it("admits up to the per-caller cap, then rejects with scope/reason/retryAfterSec", () => {
    const clock = makeClock();
    // Generous per-agent, tight per-caller (cap 3, no refill within the test).
    const limiter = makeLimiter(clock.fn, {
      agentCap: 100,
      agentRefill: 100,
      callerCap: 3,
      callerRefill: 0,
    });
    const key = principalKey("https://issuer.example.com", "sub-1");

    // First 3 admit.
    for (let i = 0; i < 3; i++) {
      expect(limiter.check("agent-a", key).allowed).toBe(true);
    }

    // 4th is rejected by the per-caller scope.
    const rejected = limiter.check("agent-a", key);
    expect(rejected.allowed).toBe(false);
    expect(rejected.scope).toBe("caller");
    expect(rejected.reason).toBe("rate");
    expect(rejected.retryAfterSec).toBeGreaterThanOrEqual(1);
  });

  it("does not burn budget on rejection (a reject is idempotent)", () => {
    const clock = makeClock();
    const limiter = makeLimiter(clock.fn, {
      agentCap: 100,
      agentRefill: 100,
      callerCap: 1,
      callerRefill: 0,
    });
    const key = principalKey("iss", "sub");

    expect(limiter.check("a", key).allowed).toBe(true);
    // Repeated rejects stay rejected and never mutate into an admit.
    expect(limiter.check("a", key).allowed).toBe(false);
    expect(limiter.check("a", key).allowed).toBe(false);
  });
});

describe("InvokeRateLimiter — scope independence (Req 14.1)", () => {
  it("per-agent scope rejects independently of any single caller", () => {
    const clock = makeClock();
    // Tight per-agent (cap 2), generous per-caller: two DIFFERENT callers on the
    // same agent still exhaust the shared per-agent budget.
    const limiter = makeLimiter(clock.fn, {
      agentCap: 2,
      agentRefill: 0,
      callerCap: 100,
      callerRefill: 100,
    });

    expect(limiter.check("agent-a", principalKey("iss", "u1")).allowed).toBe(true);
    expect(limiter.check("agent-a", principalKey("iss", "u2")).allowed).toBe(true);

    // Third distinct caller is blocked by the per-AGENT scope, not per-caller.
    const rejected = limiter.check("agent-a", principalKey("iss", "u3"));
    expect(rejected.allowed).toBe(false);
    expect(rejected.scope).toBe("agent");
  });

  it("per-caller scope rejects independently of other callers on the same agent", () => {
    const clock = makeClock();
    // Generous per-agent, tight per-caller (cap 1): one caller hitting its cap
    // does not block a different caller on the same agent.
    const limiter = makeLimiter(clock.fn, {
      agentCap: 100,
      agentRefill: 100,
      callerCap: 1,
      callerRefill: 0,
    });

    const u1 = principalKey("iss", "u1");
    const u2 = principalKey("iss", "u2");

    expect(limiter.check("agent-a", u1).allowed).toBe(true);
    // u1 is now capped...
    const u1Rejected = limiter.check("agent-a", u1);
    expect(u1Rejected.allowed).toBe(false);
    expect(u1Rejected.scope).toBe("caller");
    // ...but u2 is unaffected.
    expect(limiter.check("agent-a", u2).allowed).toBe(true);
  });

  it("distinguishes the same sub across different issuers (issuer-qualified key)", () => {
    const clock = makeClock();
    const limiter = makeLimiter(clock.fn, {
      agentCap: 100,
      agentRefill: 100,
      callerCap: 1,
      callerRefill: 0,
    });

    expect(limiter.check("agent-a", principalKey("iss-1", "same-sub")).allowed).toBe(true);
    // Same sub, different issuer → a distinct per-caller bucket, still admitted.
    expect(limiter.check("agent-a", principalKey("iss-2", "same-sub")).allowed).toBe(true);
  });
});

describe("InvokeRateLimiter — refill over injected time re-admits (Req 14.1)", () => {
  it("re-admits after enough injected time has elapsed to refill a token", () => {
    const clock = makeClock();
    // cap 1, refill 1 token/sec.
    const limiter = makeLimiter(clock.fn, {
      agentCap: 100,
      agentRefill: 100,
      callerCap: 1,
      callerRefill: 1,
    });
    const key = principalKey("iss", "sub");

    expect(limiter.check("a", key).allowed).toBe(true);
    const rejected = limiter.check("a", key);
    expect(rejected.allowed).toBe(false);
    // The advertised Retry-After should be enough to refill one token.
    const wait = rejected.retryAfterSec ?? 1;
    expect(wait).toBeGreaterThanOrEqual(1);

    // Not enough time yet.
    clock.advanceSec(wait - 0.5);
    expect(limiter.check("a", key).allowed).toBe(false);

    // After the full advertised wait, a token has refilled → re-admitted.
    clock.advanceSec(1);
    expect(limiter.check("a", key).allowed).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Layer 2 — integration: limiter wired into the real invoke handler
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

function makeUpstream(chunks: string[]): PassThrough {
  const pt = new PassThrough() as PassThrough & {
    statusCode?: number;
    headers?: Record<string, string>;
  };
  pt.statusCode = 200;
  pt.headers = { "content-type": "text/event-stream" };
  setImmediate(() => {
    for (const c of chunks) pt.write(c);
    pt.end();
  });
  return pt;
}

const servers: http.Server[] = [];

function mount(deps: InvokeHandlerDeps): Promise<string> {
  const app = express();
  app.post("/invoke/:agentId", createInvokeHandler(deps));
  return listenLoopback(app, servers);
}

afterEach(async () => {
  await closeServers(servers);
  vi.restoreAllMocks();
});

describe("POST /invoke/:agentId — 429 on over-limit, never dispatches (Req 14.1, 14.2)", () => {
  it("admits the first request then returns 429 with Retry-After and no dispatch on the second", async () => {
    // Per-caller cap of 1, no refill → the 2nd call from the same principal is
    // rejected before dispatch. Per-agent kept generous so the reject is the
    // per-caller scope.
    const limiter = new InvokeRateLimiter({
      perAgent: { burst: { capacity: 1000, refillPerSec: 1000 } },
      perCaller: { burst: { capacity: 1, refillPerSec: 0, windowFallbackSec: 30 } },
    });

    const dispatches: string[] = [];
    const signedRequestStream: SignedRequestStreamFn = async (url) => {
      dispatches.push(url);
      return makeUpstream(["event: text\ndata: hi\n\n"]) as any;
    };

    const deps: InvokeHandlerDeps = {
      invokeTaskUrl: TASK_URL,
      assetIndexReady: true,
      resolveCurrentPublishedAgent: async () => makeAgentRow(),
      verifyAgentToken: async () => ({ sub: CALLER_SUB, clientId: null }),
      signedRequestStream,
      rateLimiter: limiter,
    };
    const base = await mount(deps);

    const doPost = () =>
      fetch(`${base}/invoke/agent-xyz`, {
        method: "POST",
        headers: {
          authorization: "Bearer good.jwt.token",
          "content-type": "application/json",
        },
        body: JSON.stringify({ prompt: "hello" }),
      });

    // 1st request: admitted, dispatched, SSE stream.
    const first = await doPost();
    expect(first.status).toBe(200);
    expect(first.headers.get("content-type")).toBe("text/event-stream");
    await first.text();

    // 2nd request from the same caller: over the per-caller cap → 429.
    const second = await doPost();
    expect(second.status).toBe(429);
    const retryAfter = second.headers.get("retry-after");
    expect(retryAfter).not.toBeNull();
    expect(Number(retryAfter)).toBeGreaterThanOrEqual(1);
    const body = await second.json();
    expect(body.error.category).toBe("rate_limited");

    // Only the admitted request ever dispatched to the runtime.
    expect(dispatches).toHaveLength(1);
  });

  it("returns 429 before dispatch when the per-agent limit is exhausted", async () => {
    // Per-agent cap of 0 → every request is rejected by the per-agent scope
    // regardless of caller, and NEVER dispatches.
    const limiter = new InvokeRateLimiter({
      perAgent: { burst: { capacity: 0, refillPerSec: 0, windowFallbackSec: 60 } },
      perCaller: { burst: { capacity: 1000, refillPerSec: 1000 } },
    });

    const dispatches: string[] = [];
    const deps: InvokeHandlerDeps = {
      invokeTaskUrl: TASK_URL,
      assetIndexReady: true,
      resolveCurrentPublishedAgent: async () => makeAgentRow(),
      verifyAgentToken: async () => ({ sub: CALLER_SUB, clientId: null }),
      signedRequestStream: async (url) => {
        dispatches.push(url);
        return makeUpstream(["event: text\ndata: hi\n\n"]) as any;
      },
      rateLimiter: limiter,
    };
    const base = await mount(deps);

    const res = await fetch(`${base}/invoke/agent-xyz`, {
      method: "POST",
      headers: { authorization: "Bearer good.jwt.token" },
      body: JSON.stringify({ prompt: "hello" }),
    });

    expect(res.status).toBe(429);
    expect(res.headers.get("retry-after")).not.toBeNull();
    expect(dispatches).toHaveLength(0); // never dispatched
  });
});
