// Unit tests for the streaming transport reliability primitives
// (`./streamGuard`) — task 15.2, Requirements 15.1 / 15.3 / 15.4.
//
// These exercise the two pure, side-effect-free units directly:
//   * createStreamGuard / streamGuardFromEnv — per-instance concurrency cap
//     (Req 15.4): tryAcquire up to cap then reject, release frees a slot,
//     release-below-zero is safe, cap=Infinity never blocks.
//   * DEFAULT_TRANSPORT_CONFIG — keepalive-within-idle-window invariant
//     (Req 15.1): keepalive < proxyIdle ≤ edgeIdle.
//   * normalizeTransportConfig — timeout-ordering clamp (Req 15.3): any
//     violating combination is clamped to keepalive < proxyIdle ≤ edgeIdle and
//     runtime ≤ proxyIdle (warning once); a valid combo is left unchanged.
//
// Validates: Requirements 15.1, 15.3, 15.4

import { describe, it, expect, vi } from "vitest";
import {
  createStreamGuard,
  streamGuardFromEnv,
  normalizeTransportConfig,
  transportConfigFromEnv,
  DEFAULT_TRANSPORT_CONFIG,
  type TransportConfig,
} from "./streamGuard";

// ---------------------------------------------------------------------------
// 15.4 — concurrency cap (createStreamGuard / streamGuardFromEnv)
// ---------------------------------------------------------------------------

describe("createStreamGuard — per-instance concurrency cap (Req 15.4)", () => {
  it("admits up to the cap then rejects the next tryAcquire", () => {
    const guard = createStreamGuard(2);
    expect(guard.cap).toBe(2);
    expect(guard.active).toBe(0);

    expect(guard.tryAcquire()).toBe(true); // 1
    expect(guard.tryAcquire()).toBe(true); // 2
    expect(guard.active).toBe(2);

    // At capacity → reject, without incrementing.
    expect(guard.tryAcquire()).toBe(false);
    expect(guard.active).toBe(2);
  });

  it("release frees a slot so a subsequent tryAcquire is admitted again", () => {
    const guard = createStreamGuard(1);
    expect(guard.tryAcquire()).toBe(true);
    expect(guard.tryAcquire()).toBe(false); // at cap

    guard.release();
    expect(guard.active).toBe(0);
    expect(guard.tryAcquire()).toBe(true); // slot freed → admitted
  });

  it("release below zero is safe and never drives the counter negative", () => {
    const guard = createStreamGuard(1);
    // Extra releases with nothing in flight must be no-ops.
    guard.release();
    guard.release();
    expect(guard.active).toBe(0);

    // The cap is still fully usable after spurious releases.
    expect(guard.tryAcquire()).toBe(true);
    expect(guard.active).toBe(1);
    expect(guard.tryAcquire()).toBe(false);
  });

  it("a double-release cannot wedge the cap open (counter floors at zero)", () => {
    const guard = createStreamGuard(1);
    expect(guard.tryAcquire()).toBe(true);
    guard.release();
    guard.release(); // spurious second release
    expect(guard.active).toBe(0);
    // Cap is still enforced: exactly one slot, not two.
    expect(guard.tryAcquire()).toBe(true);
    expect(guard.tryAcquire()).toBe(false);
  });

  it("cap of Infinity (or 0/negative) disables the guard — never blocks", () => {
    for (const cap of [Infinity, 0, -5]) {
      const guard = createStreamGuard(cap);
      expect(guard.cap).toBe(Infinity);
      for (let i = 0; i < 1000; i += 1) {
        expect(guard.tryAcquire()).toBe(true);
      }
      expect(guard.active).toBe(1000);
    }
  });

  it("streamGuardFromEnv defaults to 200 and maps 0 → unbounded", () => {
    expect(streamGuardFromEnv({}).cap).toBe(200);
    expect(streamGuardFromEnv({ INVOKE_MAX_CONCURRENT_STREAMS: "5" }).cap).toBe(5);
    expect(streamGuardFromEnv({ INVOKE_MAX_CONCURRENT_STREAMS: "0" }).cap).toBe(Infinity);
    // Garbage falls back to the default.
    expect(streamGuardFromEnv({ INVOKE_MAX_CONCURRENT_STREAMS: "nope" }).cap).toBe(200);
  });
});

// ---------------------------------------------------------------------------
// 15.1 — keepalive strictly within the idle window (default config invariant)
// ---------------------------------------------------------------------------

describe("DEFAULT_TRANSPORT_CONFIG — keepalive within idle window (Req 15.1)", () => {
  it("keeps keepalive < proxyIdle ≤ edgeIdle and runtime ≤ proxyIdle", () => {
    const {
      keepaliveMs,
      proxyIdleTimeoutMs,
      edgeIdleTimeoutMs,
      runtimeStreamTimeoutMs,
    } = DEFAULT_TRANSPORT_CONFIG;

    expect(keepaliveMs).toBeLessThan(proxyIdleTimeoutMs);
    expect(proxyIdleTimeoutMs).toBeLessThanOrEqual(edgeIdleTimeoutMs);
    expect(runtimeStreamTimeoutMs).toBeLessThanOrEqual(proxyIdleTimeoutMs);
  });
});

// ---------------------------------------------------------------------------
// 15.3 — timeout ordering: normalizeTransportConfig clamps violations
// ---------------------------------------------------------------------------

/** Assert the ordering invariant holds for a normalized config. */
function expectOrderingHolds(cfg: TransportConfig): void {
  // keepalive < proxy (when proxy is a real bound).
  if (cfg.proxyIdleTimeoutMs > 0) {
    expect(cfg.keepaliveMs).toBeLessThan(cfg.proxyIdleTimeoutMs);
  }
  // proxy ≤ edge (when edge is a real bound).
  if (cfg.edgeIdleTimeoutMs > 0) {
    expect(cfg.proxyIdleTimeoutMs).toBeLessThanOrEqual(cfg.edgeIdleTimeoutMs);
  }
  // runtime ≤ proxy (when proxy is a real bound).
  if (cfg.proxyIdleTimeoutMs > 0) {
    expect(cfg.runtimeStreamTimeoutMs).toBeLessThanOrEqual(cfg.proxyIdleTimeoutMs);
  }
}

describe("normalizeTransportConfig — timeout ordering edge ≥ proxy ≥ runtime (Req 15.3)", () => {
  it("clamps proxy down to edge when proxy > edge, warning once", () => {
    const warn = vi.fn();
    const out = normalizeTransportConfig(
      {
        keepaliveMs: 5_000,
        proxyIdleTimeoutMs: 90_000, // > edge
        edgeIdleTimeoutMs: 60_000,
        runtimeStreamTimeoutMs: 10_000,
      },
      warn,
    );
    expect(out.proxyIdleTimeoutMs).toBe(60_000);
    expectOrderingHolds(out);
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it("raises proxy up to runtime when runtime > proxy, warning once", () => {
    const warn = vi.fn();
    const out = normalizeTransportConfig(
      {
        keepaliveMs: 5_000,
        proxyIdleTimeoutMs: 30_000,
        edgeIdleTimeoutMs: 120_000,
        runtimeStreamTimeoutMs: 50_000, // > proxy
      },
      warn,
    );
    expect(out.proxyIdleTimeoutMs).toBe(50_000);
    expect(out.runtimeStreamTimeoutMs).toBeLessThanOrEqual(out.proxyIdleTimeoutMs);
    expectOrderingHolds(out);
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it("lowers keepalive below proxy when keepalive >= proxy, warning once", () => {
    const warn = vi.fn();
    const out = normalizeTransportConfig(
      {
        keepaliveMs: 60_000, // >= proxy
        proxyIdleTimeoutMs: 60_000,
        edgeIdleTimeoutMs: 120_000,
        runtimeStreamTimeoutMs: 10_000,
      },
      warn,
    );
    expect(out.keepaliveMs).toBeLessThan(out.proxyIdleTimeoutMs);
    expectOrderingHolds(out);
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it("clamps a multi-violation combo into a fully-valid ordering", () => {
    // proxy > edge AND keepalive >= proxy simultaneously; runtime kept ≤ edge so
    // the input is internally satisfiable (runtime > edge is contradictory: it
    // can't satisfy runtime ≤ proxy ≤ edge, and the normalizer intentionally
    // favors proxy ≥ runtime in that case).
    const warn = vi.fn();
    const out = normalizeTransportConfig(
      {
        keepaliveMs: 100_000, // >= proxy
        proxyIdleTimeoutMs: 80_000, // > edge
        edgeIdleTimeoutMs: 60_000,
        runtimeStreamTimeoutMs: 50_000, // ≤ edge (satisfiable)
      },
      warn,
    );
    expectOrderingHolds(out);
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it("leaves a valid combination unchanged and does not warn", () => {
    const warn = vi.fn();
    const valid: TransportConfig = {
      keepaliveMs: 15_000,
      proxyIdleTimeoutMs: 60_000,
      edgeIdleTimeoutMs: 60_000,
      runtimeStreamTimeoutMs: 50_000,
    };
    const out = normalizeTransportConfig(valid, warn);
    expect(out).toEqual(valid);
    expect(warn).not.toHaveBeenCalled();
  });

  it("transportConfigFromEnv returns a normalized config satisfying the invariant", () => {
    // Violating env combo should be clamped by the from-env normalizer.
    const cfg = transportConfigFromEnv({
      INVOKE_SSE_KEEPALIVE_MS: "90000",
      INVOKE_PROXY_IDLE_TIMEOUT_MS: "90000",
      INVOKE_EDGE_IDLE_TIMEOUT_MS: "60000",
      INVOKE_RUNTIME_STREAM_TIMEOUT_MS: "10000",
    });
    expectOrderingHolds(cfg);
    // Defaults path also satisfies the invariant.
    expectOrderingHolds(transportConfigFromEnv({}));
  });
});
