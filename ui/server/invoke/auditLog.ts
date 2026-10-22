/**
 * Structured audit logging for the external invoke path
 * (`POST /invoke/:agentId`) — task 17.1, Requirement 17.1.
 *
 * EVERY external invocation writes exactly one structured audit entry recording
 * `agentId`, the resolved published version, the Owner, the Caller `sub`, and
 * the outcome — and entries are emitted for REJECTED requests too (401/403/404/
 * 429/503) so denied attempts are traceable, not just successes (design §Req 17).
 *
 * ── Secret hygiene (Req 18.2) ───────────────────────────────────────────────
 * The entry records the caller's SUBJECT (`sub`) as its identity handle — never
 * the bearer token, Authorization header, or any credential. The audit sink is
 * a narrow, typed shape ({@link AuditEntry}); callers cannot smuggle a raw token
 * into it, so audit output never leaks secrets.
 *
 * The sink is injected (an {@link AuditLogger}) so the proxy wires a real
 * structured-log writer while tests capture entries in-memory. The default sink
 * emits one JSON line per entry to stdout under a stable `[agentcore-audit]`
 * prefix, ready for log-based ingestion (CloudWatch → metric filters/queries).
 */

/**
 * Coarse outcome class, derived from the HTTP status the caller received:
 *   * `allowed` — 2xx (the invocation was dispatched / streamed).
 *   * `denied`  — a 4xx gate rejection (kill switch/429/404/403/401).
 *   * `error`   — a 5xx or a post-dispatch stream failure.
 */
export type AuditDisposition = "allowed" | "denied" | "error";

/** The identity/version context known about an invocation when it is audited. */
export interface AuditContext {
  /** Path `agentId` (always known). */
  agentId: string;
  /** Resolved published version, when the agent row was resolved. */
  publishedVersion?: string | number | null;
  /** Owner (resource actor) user id, when the agent row was resolved. */
  owner?: string | null;
  /** Issuer-qualified caller subject, when the token was verified. */
  callerSub?: string | null;
  /** Caller token issuer, when the integration config was resolved. */
  issuer?: string | null;
}

/** The structured, secret-free audit record written per external invocation. */
export interface AuditEntry extends Required<AuditContext> {
  /** ISO-8601 emit time. */
  timestamp: string;
  /** Stable event discriminator for log queries. */
  event: "external_invoke";
  /** The resolved outcome. */
  outcome: {
    disposition: AuditDisposition;
    /** HTTP status the caller received (or a synthetic status for stream fails). */
    status: number;
    /**
     * Fine-grained outcome category — the error-taxonomy category
     * (`not_found`, `rate_limited`, …), a kill-switch category
     * (`kill_switch_global`/`kill_switch_agent`), `success`, or a post-dispatch
     * stream category (`stream_failed`/`dispatch_failed`/`client_closed`).
     */
    category: string;
  };
}

/** Where audit entries go. Injected so tests capture instead of writing logs. */
export interface AuditLogger {
  record(entry: AuditEntry): void;
}

/** Map an HTTP status to its coarse audit disposition. */
export function dispositionForStatus(status: number): AuditDisposition {
  if (status >= 200 && status < 300) return "allowed";
  if (status >= 500) return "error";
  return "denied";
}

/**
 * Build a fully-populated {@link AuditEntry} from partial context + an outcome,
 * defaulting every unknown field to `null` and deriving the disposition from
 * the status unless one is explicitly supplied. Kept pure for easy testing.
 */
export function buildAuditEntry(
  ctx: AuditContext,
  outcome: { status: number; category: string; disposition?: AuditDisposition },
  now: () => Date = () => new Date(),
): AuditEntry {
  return {
    timestamp: now().toISOString(),
    event: "external_invoke",
    agentId: ctx.agentId,
    publishedVersion: ctx.publishedVersion ?? null,
    owner: ctx.owner ?? null,
    callerSub: ctx.callerSub ?? null,
    issuer: ctx.issuer ?? null,
    outcome: {
      disposition: outcome.disposition ?? dispositionForStatus(outcome.status),
      status: outcome.status,
      category: outcome.category,
    },
  };
}

/**
 * Default audit sink: one JSON line per entry to stdout under a stable prefix
 * so log ingestion can filter/parse it. Never prints tokens (the entry type
 * has no field for one).
 */
export function createConsoleAuditLogger(
  write: (line: string) => void = (l) => console.log(l),
): AuditLogger {
  return {
    record(entry: AuditEntry): void {
      try {
        write(`[agentcore-audit] ${JSON.stringify(entry)}`);
      } catch {
        /* audit logging must never throw into the request path */
      }
    },
  };
}
