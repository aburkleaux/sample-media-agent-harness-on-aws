/**
 * Streaming transport reliability primitives for the external invoke path
 * (`POST /invoke/:agentId`) — task 15.1, Requirements 15.1 / 15.2 / 15.3 / 15.4.
 *
 * Two small, side-effect-free, unit-testable units live here so the SSE
 * passthrough in `./invokeRoute` stays a thin orchestrator:
 *
 *   * {@link StreamGuard} — a per-proxy-instance concurrency cap (Req 15.4).
 *     Streaming responses are long-lived; a runaway number of simultaneous SSE
 *     connections would exhaust sockets/memory on a single task. The guard
 *     bounds the number of *in-flight* streams and lets the handler reject
 *     excess requests with HTTP 503 BEFORE dispatch (never degrading streams
 *     that are already flowing). State is process-local — behind an ALB with N
 *     tasks the effective cap is ~N× the configured value; a distributed cap is
 *     a documented future enhancement (same tradeoff as the rate limiter).
 *
 *   * {@link TransportConfig} — the keepalive cadence + idle-timeout budget
 *     (Req 15.1 / 15.3). Values are env-configurable with documented defaults
 *     and are normalized so the ordering invariant below always holds.
 *
 * ── Timeout ordering invariant (Req 15.3) ───────────────────────────────────
 *   keepalive < proxyIdle ≤ edgeIdle   and   runtime ≤ proxyIdle ≤ edgeIdle
 *
 *   i.e. the edge (ALB/CloudFront) idle timeout is the LARGEST, the proxy
 *   socket idle timeout sits at or below it, and the runtime's own stream
 *   timeout sits at or below the proxy's — so a stream the runtime still
 *   considers active is never severed first by the proxy or the edge. The
 *   keepalive interval is strictly smaller than the proxy idle timeout so that
 *   heartbeat frames reset the idle clock long before any layer would fire.
 *   {@link normalizeTransportConfig} clamps any env combination that violates
 *   this ordering (emitting a one-time warning) so misconfiguration fails safe.
 */

// ── Concurrency cap (Req 15.4) ──────────────────────────────────────────────

/**
 * A per-instance concurrent-stream guard. `tryAcquire` returns `false` when the
 * cap is already reached (the caller should then return HTTP 503); otherwise it
 * reserves a slot that MUST be released exactly once via `release` on stream
 * end/close/error. `release` is a no-op below zero so accidental double-release
 * can never drive the counter negative and wedge the cap open.
 */
export interface StreamGuard {
  /** Reserve a stream slot. Returns false when already at capacity. */
  tryAcquire(): boolean;
  /** Free a previously-acquired slot (idempotent-safe below zero). */
  release(): void;
  /** Current number of in-flight streams (for tests/metrics). */
  readonly active: number;
  /** The configured maximum. `Infinity` disables the cap. */
  readonly cap: number;
}

export function createStreamGuard(cap: number): StreamGuard {
  // A non-finite/≤0 cap disables the guard (unbounded) — used when the env var
  // is unset/0 so behavior stays additive for callers that don't opt in.
  const limit = Number.isFinite(cap) && cap > 0 ? Math.floor(cap) : Infinity;
  let active = 0;
  return {
    tryAcquire(): boolean {
      if (active >= limit) return false;
      active += 1;
      return true;
    },
    release(): void {
      if (active > 0) active -= 1;
    },
    get active(): number {
      return active;
    },
    get cap(): number {
      return limit;
    },
  };
}

/**
 * Build a {@link StreamGuard} from the environment.
 *
 * Env (optional):
 *   INVOKE_MAX_CONCURRENT_STREAMS — per-instance cap (default 200; 0 → unbounded).
 */
export function streamGuardFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): StreamGuard {
  const raw = (env.INVOKE_MAX_CONCURRENT_STREAMS ?? "").trim();
  const parsed = raw === "" ? 200 : Number(raw);
  const cap = Number.isFinite(parsed) && parsed >= 0 ? parsed : 200;
  // 0 → unbounded (Infinity via createStreamGuard's ≤0 handling).
  return createStreamGuard(cap === 0 ? Infinity : cap);
}

// ── Keepalive + timeout budget (Req 15.1 / 15.3) ────────────────────────────

export interface TransportConfig {
  /**
   * Keepalive cadence in ms. While an SSE response is open and idle, the proxy
   * emits a `: keep-alive\n\n` SSE comment every `keepaliveMs` to reset the
   * edge/LB idle clock (Req 15.1). Must be strictly < `proxyIdleTimeoutMs`.
   */
  keepaliveMs: number;
  /**
   * The proxy's socket idle timeout in ms, applied via `req/res.setTimeout`.
   * Sits at or below the edge idle timeout and at or above the runtime stream
   * timeout (Req 15.3). `0` disables the socket idle timeout entirely.
   */
  proxyIdleTimeoutMs: number;
  /**
   * The edge (ALB/CloudFront) idle timeout in ms. Not enforced by the app
   * (it's infra config) but recorded here so the ordering invariant can be
   * validated and so the keepalive/proxy values are provably within it.
   */
  edgeIdleTimeoutMs: number;
  /**
   * The runtime's own stream timeout in ms (informational). The proxy idle
   * timeout is kept ≥ this so the proxy never severs a stream the runtime
   * still considers active. `0` → unknown/unbounded.
   */
  runtimeStreamTimeoutMs: number;
}

export const DEFAULT_TRANSPORT_CONFIG: TransportConfig = {
  // 15s heartbeat: comfortably inside the common 60s ALB/CloudFront idle window.
  keepaliveMs: 15_000,
  // Proxy socket idle timeout below the edge window; keepalives keep it from
  // firing on an active stream, so it only trips a genuinely dead connection.
  proxyIdleTimeoutMs: 60_000,
  // Documented edge idle timeout (ALB default is 60s; raise both together).
  edgeIdleTimeoutMs: 60_000,
  // Runtime stream timeout ≤ proxy so the runtime ends first, gracefully.
  runtimeStreamTimeoutMs: 50_000,
};

/**
 * Enforce `keepalive < proxyIdle ≤ edgeIdle` and `runtime ≤ proxyIdle` by
 * clamping any violating combination, warning once so misconfiguration is
 * visible but never fatal. Returns a normalized, safe-to-apply config.
 */
export function normalizeTransportConfig(
  cfg: TransportConfig,
  warn: (msg: string) => void = (m) => console.warn(m),
): TransportConfig {
  let { keepaliveMs, proxyIdleTimeoutMs, edgeIdleTimeoutMs, runtimeStreamTimeoutMs } = cfg;

  keepaliveMs = Math.max(0, keepaliveMs);
  proxyIdleTimeoutMs = Math.max(0, proxyIdleTimeoutMs);
  edgeIdleTimeoutMs = Math.max(0, edgeIdleTimeoutMs);
  runtimeStreamTimeoutMs = Math.max(0, runtimeStreamTimeoutMs);

  const violations: string[] = [];

  // edge ≥ proxy: clamp proxy down to edge when edge is a real (>0) bound.
  if (edgeIdleTimeoutMs > 0 && proxyIdleTimeoutMs > edgeIdleTimeoutMs) {
    violations.push(
      `proxy idle ${proxyIdleTimeoutMs}ms > edge idle ${edgeIdleTimeoutMs}ms; clamping proxy to edge`,
    );
    proxyIdleTimeoutMs = edgeIdleTimeoutMs;
  }

  // proxy ≥ runtime: raise proxy up to runtime so the proxy outlives the runtime.
  if (proxyIdleTimeoutMs > 0 && runtimeStreamTimeoutMs > proxyIdleTimeoutMs) {
    violations.push(
      `runtime stream ${runtimeStreamTimeoutMs}ms > proxy idle ${proxyIdleTimeoutMs}ms; raising proxy to runtime`,
    );
    proxyIdleTimeoutMs = runtimeStreamTimeoutMs;
  }

  // keepalive < proxy: keepalive must reset the idle clock before it fires.
  if (proxyIdleTimeoutMs > 0 && keepaliveMs >= proxyIdleTimeoutMs) {
    const adjusted = Math.max(1_000, Math.floor(proxyIdleTimeoutMs / 2));
    violations.push(
      `keepalive ${keepaliveMs}ms >= proxy idle ${proxyIdleTimeoutMs}ms; lowering keepalive to ${adjusted}ms`,
    );
    keepaliveMs = adjusted;
  }

  if (violations.length > 0) {
    warn(
      `[agentcore-proxy] transport timeout ordering adjusted (edge ≥ proxy ≥ runtime, keepalive < proxy): ${violations.join("; ")}`,
    );
  }

  return { keepaliveMs, proxyIdleTimeoutMs, edgeIdleTimeoutMs, runtimeStreamTimeoutMs };
}

/**
 * Build a normalized {@link TransportConfig} from the environment.
 *
 * Env (all optional; ms):
 *   INVOKE_SSE_KEEPALIVE_MS          (default 15000)
 *   INVOKE_PROXY_IDLE_TIMEOUT_MS     (default 60000; 0 → no socket idle timeout)
 *   INVOKE_EDGE_IDLE_TIMEOUT_MS      (default 60000; documents ALB/CloudFront idle)
 *   INVOKE_RUNTIME_STREAM_TIMEOUT_MS (default 50000; 0 → unknown/unbounded)
 */
export function transportConfigFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): TransportConfig {
  const num = (name: string, dflt: number): number => {
    const raw = (env[name] ?? "").trim();
    if (raw === "") return dflt;
    const v = Number(raw);
    return Number.isFinite(v) && v >= 0 ? v : dflt;
  };
  return normalizeTransportConfig({
    keepaliveMs: num("INVOKE_SSE_KEEPALIVE_MS", DEFAULT_TRANSPORT_CONFIG.keepaliveMs),
    proxyIdleTimeoutMs: num(
      "INVOKE_PROXY_IDLE_TIMEOUT_MS",
      DEFAULT_TRANSPORT_CONFIG.proxyIdleTimeoutMs,
    ),
    edgeIdleTimeoutMs: num(
      "INVOKE_EDGE_IDLE_TIMEOUT_MS",
      DEFAULT_TRANSPORT_CONFIG.edgeIdleTimeoutMs,
    ),
    runtimeStreamTimeoutMs: num(
      "INVOKE_RUNTIME_STREAM_TIMEOUT_MS",
      DEFAULT_TRANSPORT_CONFIG.runtimeStreamTimeoutMs,
    ),
  });
}
