/**
 * In-memory rate limiter / quota gate for the external invoke path
 * (`POST /invoke/:agentId`) — task 14.1, Requirements 14.1 / 14.2.
 *
 * Enforces two independent scopes before dispatch:
 *   * **per-agent**   — keyed on `agentId` (protects the shared runtime from a
 *                       single agent's aggregate traffic).
 *   * **per-caller**  — keyed on `agentId|<issuer>#<sub>` (the issuer-qualified
 *                       Principal) so one caller cannot starve others on the
 *                       same agent.
 *
 * Each scope combines a **token bucket** (burst capacity + sustained refill
 * rate) with an optional **fixed-window quota** (max invocations per window).
 * A request is admitted only when BOTH scopes admit it; consumption is then
 * committed to both atomically so a per-caller rejection never burns the
 * per-agent budget (and vice-versa).
 *
 * NOTE (scope): this limiter is **per proxy instance** — state lives in
 * process memory. Behind an ALB with N tasks the effective limit is roughly
 * N× the configured value. A distributed store (e.g. Redis/DynamoDB counters)
 * is a documented future enhancement; the pure, clock-injectable surface here
 * is designed so that swap is localized.
 *
 * The class takes an injectable `clock` so task 14.2 can drive time
 * deterministically in tests without real timers.
 */

/** Monotonic-ish wall clock in epoch milliseconds. Injectable for tests. */
export type Clock = () => number;

/** Token-bucket parameters: burst `capacity` refilled at `refillPerSec`. */
export interface TokenBucketRule {
  /** Maximum burst — the bucket never holds more than this many tokens. */
  capacity: number;
  /** Sustained refill rate in tokens per second. */
  refillPerSec: number;
  /**
   * Escape hatch for the pathological `refillPerSec === 0` config (a hard cap
   * with no refill): the `Retry-After` to advertise since a drained bucket
   * never refills on its own. Defaults to 1 when unset.
   */
  windowFallbackSec?: number;
}

/** Fixed-window quota: at most `limit` admits per rolling `windowSec` window. */
export interface QuotaRule {
  limit: number;
  windowSec: number;
}

/** Combined rule for one scope (per-agent or per-caller). */
export interface ScopeRule {
  burst: TokenBucketRule;
  /** Optional longer-horizon cap layered on top of the burst bucket. */
  quota?: QuotaRule;
}

export interface RateLimiterOptions {
  perAgent: ScopeRule;
  perCaller: ScopeRule;
  /** Injectable clock (epoch ms). Defaults to `Date.now`. */
  clock?: Clock;
  /**
   * Soft cap on tracked keys per map to bound memory. When exceeded, the
   * least-recently-touched entries are pruned. Defaults to 10_000.
   */
  maxEntries?: number;
}

export type LimitScope = "agent" | "caller";
export type LimitReason = "rate" | "quota";

export interface RateLimitResult {
  allowed: boolean;
  /** Which scope rejected (only set when `allowed === false`). */
  scope?: LimitScope;
  /** Whether the burst bucket or the fixed-window quota rejected. */
  reason?: LimitReason;
  /** Suggested `Retry-After` in whole seconds (>= 1) when rejected. */
  retryAfterSec?: number;
}

interface BucketState {
  tokens: number;
  last: number; // epoch ms of last refill
}

interface QuotaState {
  count: number;
  windowStart: number; // epoch ms
}

/** Result of a non-committing scope evaluation. */
interface ScopeEval {
  ok: boolean;
  reason?: LimitReason;
  retryAfterSec?: number;
}

/**
 * Build the issuer-qualified per-caller key component. Kept as a tiny pure
 * helper so the proxy and tests derive the exact same principal key.
 */
export function principalKey(issuer: string, sub: string): string {
  return `${issuer}#${sub}`;
}

export class InvokeRateLimiter {
  private readonly perAgent: ScopeRule;
  private readonly perCaller: ScopeRule;
  private readonly clock: Clock;
  private readonly maxEntries: number;

  private readonly agentBuckets = new Map<string, BucketState>();
  private readonly agentQuota = new Map<string, QuotaState>();
  private readonly callerBuckets = new Map<string, BucketState>();
  private readonly callerQuota = new Map<string, QuotaState>();

  constructor(opts: RateLimiterOptions) {
    this.perAgent = opts.perAgent;
    this.perCaller = opts.perCaller;
    this.clock = opts.clock ?? Date.now;
    this.maxEntries = opts.maxEntries ?? 10_000;
  }

  /**
   * Evaluate and (when admitted) consume one unit from both the per-agent and
   * per-caller scopes. Returns `{ allowed: true }` when both admit; otherwise
   * `{ allowed: false, scope, reason, retryAfterSec }` naming the first scope
   * that rejected (per-agent checked first). No budget is consumed on reject.
   */
  check(agentId: string, callerPrincipalKey: string): RateLimitResult {
    const now = this.clock();
    const callerKey = `${agentId}|${callerPrincipalKey}`;

    // Refill/roll windows for BOTH scopes first (idempotent given `now`), then
    // evaluate without consuming so a reject never mutates budgets.
    const agent = this.evaluateScope(
      this.agentBuckets,
      this.agentQuota,
      agentId,
      this.perAgent,
      now,
    );
    if (!agent.ok) {
      return {
        allowed: false,
        scope: "agent",
        reason: agent.reason,
        retryAfterSec: agent.retryAfterSec,
      };
    }

    const caller = this.evaluateScope(
      this.callerBuckets,
      this.callerQuota,
      callerKey,
      this.perCaller,
      now,
    );
    if (!caller.ok) {
      return {
        allowed: false,
        scope: "caller",
        reason: caller.reason,
        retryAfterSec: caller.retryAfterSec,
      };
    }

    // Both scopes admit — commit consumption atomically.
    this.commitScope(this.agentBuckets, this.agentQuota, agentId, this.perAgent);
    this.commitScope(this.callerBuckets, this.callerQuota, callerKey, this.perCaller);

    this.prune(this.agentBuckets);
    this.prune(this.callerBuckets);

    return { allowed: true };
  }

  /**
   * Non-consuming per-agent pre-check (task 19.1). Evaluates ONLY the per-agent
   * scope (keyed on `agentId` alone) at the current clock WITHOUT committing any
   * consumption, so it can be hoisted ahead of the expensive resolve + SSRF-
   * hardened OIDC verify steps in the fixed middleware order (design Component 4,
   * step 2) to shed abusive per-agent load early. It needs no verified principal.
   *
   * This peek NEVER burns budget and NEVER touches the per-caller scope — the
   * authoritative, atomic consumption of BOTH scopes still happens in
   * {@link check} after verification (step 6), where the per-caller key requires
   * the verified issuer-qualified principal. Returns `{ allowed: true }` when the
   * per-agent scope would currently admit, else a rejection naming
   * `scope: "agent"` with the `reason`/`retryAfterSec` hint.
   */
  peekAgent(agentId: string): RateLimitResult {
    const now = this.clock();
    const agent = this.evaluateScope(
      this.agentBuckets,
      this.agentQuota,
      agentId,
      this.perAgent,
      now,
    );
    if (!agent.ok) {
      return {
        allowed: false,
        scope: "agent",
        reason: agent.reason,
        retryAfterSec: agent.retryAfterSec,
      };
    }
    return { allowed: true };
  }

  // --- internals -----------------------------------------------------------

  /** Refill a token bucket to `now` (mutates), returning available tokens. */
  private refill(
    map: Map<string, BucketState>,
    key: string,
    rule: TokenBucketRule,
    now: number,
  ): BucketState {
    let b = map.get(key);
    if (!b) {
      b = { tokens: rule.capacity, last: now };
      map.set(key, b);
      return b;
    }
    const elapsedSec = Math.max(0, (now - b.last) / 1000);
    b.tokens = Math.min(rule.capacity, b.tokens + elapsedSec * rule.refillPerSec);
    b.last = now;
    return b;
  }

  /** Roll a fixed-window quota to `now` (mutates), returning current state. */
  private rollQuota(
    map: Map<string, QuotaState>,
    key: string,
    rule: QuotaRule,
    now: number,
  ): QuotaState {
    let q = map.get(key);
    if (!q || now - q.windowStart >= rule.windowSec * 1000) {
      q = { count: 0, windowStart: now };
      map.set(key, q);
    }
    return q;
  }

  private evaluateScope(
    bucketMap: Map<string, BucketState>,
    quotaMap: Map<string, QuotaState>,
    key: string,
    rule: ScopeRule,
    now: number,
  ): ScopeEval {
    const b = this.refill(bucketMap, key, rule.burst, now);
    if (b.tokens < 1) {
      const deficit = 1 - b.tokens;
      const retry =
        rule.burst.refillPerSec > 0
          ? Math.ceil(deficit / rule.burst.refillPerSec)
          : rule.burst.windowFallbackSec ?? 1;
      return { ok: false, reason: "rate", retryAfterSec: Math.max(1, retry) };
    }
    if (rule.quota) {
      const q = this.rollQuota(quotaMap, key, rule.quota, now);
      if (q.count >= rule.quota.limit) {
        const msLeft = rule.quota.windowSec * 1000 - (now - q.windowStart);
        return {
          ok: false,
          reason: "quota",
          retryAfterSec: Math.max(1, Math.ceil(msLeft / 1000)),
        };
      }
    }
    return { ok: true };
  }

  private commitScope(
    bucketMap: Map<string, BucketState>,
    quotaMap: Map<string, QuotaState>,
    key: string,
    rule: ScopeRule,
  ): void {
    const b = bucketMap.get(key);
    if (b) b.tokens -= 1;
    if (rule.quota) {
      const q = quotaMap.get(key);
      if (q) q.count += 1;
    }
  }

  /** Bound memory: when a map grows past `maxEntries`, drop the oldest keys. */
  private prune(map: Map<string, BucketState>): void {
    if (map.size <= this.maxEntries) return;
    const excess = map.size - this.maxEntries;
    const oldest = [...map.entries()]
      .sort((a, b) => a[1].last - b[1].last)
      .slice(0, excess);
    for (const [k] of oldest) map.delete(k);
  }
}

/**
 * Parse limiter configuration from environment variables with sane defaults.
 * Defaults are intentionally generous to avoid throttling legitimate traffic
 * while still capping runaway/abusive callers.
 *
 * Env vars (all optional):
 *   INVOKE_RL_AGENT_CAPACITY / INVOKE_RL_AGENT_REFILL_PER_SEC
 *   INVOKE_RL_AGENT_QUOTA / INVOKE_RL_AGENT_QUOTA_WINDOW_SEC
 *   INVOKE_RL_CALLER_CAPACITY / INVOKE_RL_CALLER_REFILL_PER_SEC
 *   INVOKE_RL_CALLER_QUOTA / INVOKE_RL_CALLER_QUOTA_WINDOW_SEC
 */
export function rateLimiterOptionsFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): RateLimiterOptions {
  const num = (name: string, dflt: number): number => {
    const raw = (env[name] ?? "").trim();
    if (raw === "") return dflt;
    const v = Number(raw);
    return Number.isFinite(v) && v >= 0 ? v : dflt;
  };
  const quota = (
    limitName: string,
    windowName: string,
    dfltLimit: number,
    dfltWindow: number,
  ): QuotaRule | undefined => {
    const limit = num(limitName, dfltLimit);
    if (limit <= 0) return undefined; // 0/absent → no fixed-window quota
    return { limit, windowSec: num(windowName, dfltWindow) };
  };

  return {
    perAgent: {
      burst: {
        capacity: num("INVOKE_RL_AGENT_CAPACITY", 60),
        refillPerSec: num("INVOKE_RL_AGENT_REFILL_PER_SEC", 10),
      },
      quota: quota(
        "INVOKE_RL_AGENT_QUOTA",
        "INVOKE_RL_AGENT_QUOTA_WINDOW_SEC",
        0,
        3600,
      ),
    },
    perCaller: {
      burst: {
        capacity: num("INVOKE_RL_CALLER_CAPACITY", 20),
        refillPerSec: num("INVOKE_RL_CALLER_REFILL_PER_SEC", 2),
      },
      quota: quota(
        "INVOKE_RL_CALLER_QUOTA",
        "INVOKE_RL_CALLER_QUOTA_WINDOW_SEC",
        0,
        3600,
      ),
    },
  };
}

/** Convenience factory building a limiter straight from the environment. */
export function createInvokeRateLimiter(
  env: NodeJS.ProcessEnv = process.env,
): InvokeRateLimiter {
  return new InvokeRateLimiter(rateLimiterOptionsFromEnv(env));
}
