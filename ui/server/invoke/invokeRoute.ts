/**
 * External invoke route handler — `POST /invoke/:agentId` (Req 5, 6, 8.8, 19).
 *
 * Extracted verbatim (no behavior change) from `ui/server/app.ts` into a
 * dependency-injected factory so the endpoint can be integration-tested in
 * isolation without standing up the whole Express proxy (which self-constructs
 * AWS SDK clients from env at build time). `app.ts` wires the real
 * `resolveCurrentPublishedAgent` (AssetIndex/DynamoDB), `verifyAgentToken`
 * (per-agent OIDC verifier, task 10.1), and `signedRequestStream` (SigV4 SSE
 * dispatch) into {@link createInvokeHandler}; tests inject fakes.
 *
 * The endpoint is INDEPENDENT of the internal Cognito `authMiddleware`; it uses
 * per-agent, bring-your-own OIDC verification resolved from the agent's current
 * published version's `integration_config`, then SigV4-invokes the shared Task
 * Agent runtime and streams SSE back.
 *
 * The two-identity payload stamping lives in the pure `./stampIdentity` seam
 * (Property 8, task 9.3); this module owns the request/response orchestration,
 * error taxonomy, and SSE passthrough.
 */

import type { IncomingMessage } from "http";
import {
  AgentTokenError,
  type AgentTokenErrorCategory,
  type AgentIntegrationConfig,
  type VerifiedAgentToken,
} from "../auth/agentOidc";
import { stampInvocationIdentity } from "./stampIdentity";
import { deriveExternalSessionId } from "./externalSessionKey";
import { principalKey, type InvokeRateLimiter } from "./rateLimiter";
import {
  DEFAULT_TRANSPORT_CONFIG,
  type StreamGuard,
  type TransportConfig,
} from "./streamGuard";
import type { KillSwitch } from "./killSwitch";
import {
  buildAuditEntry,
  type AuditContext,
  type AuditDisposition,
  type AuditLogger,
} from "./auditLog";

// The canonical, collision-free external-keyspace session-id derivation lives
// in `./externalSessionKey` (task 12.3). It is re-exported here so existing
// importers/tests that reference `deriveExternalSessionId` from the route
// module keep working, and so the dispatch path below can stamp it.
export { deriveExternalSessionId } from "./externalSessionKey";

// The external invoke error taxonomy (design §Error Handling). Every failure
// before dispatch returns one of these as a JSON body naming the category;
// 500/stream errors are the only ones raised after dispatch.
export type InvokeErrorCategory =
  | "not_found"
  | "client_not_allowed"
  | "rate_limited"
  | "service_unavailable"
  | "internal_error"
  | AgentTokenErrorCategory;

export const invokeErrorStatus = (category: InvokeErrorCategory): number => {
  switch (category) {
    case "not_found":
      return 404;
    case "client_not_allowed":
      return 403;
    case "rate_limited":
      return 429;
    // Per-instance concurrent-stream cap reached (Req 15.4): reject additional
    // stream requests with 503 rather than degrading in-flight streams.
    case "service_unavailable":
      return 503;
    case "internal_error":
      return 500;
    // All OIDC verifier categories map to 401.
    case "missing_token":
    case "invalid_token":
    case "expired":
    case "issuer_mismatch":
    case "audience_mismatch":
    case "jwks_unavailable":
    case "missing_subject":
      return 401;
    default:
      return 500;
  }
};

export const sendInvokeError = (
  res: any,
  category: InvokeErrorCategory,
  message: string,
  extraHeaders?: Record<string, string>,
): void => {
  const status = invokeErrorStatus(category);
  if (res.headersSent) return;
  const headers: Record<string, string> = {
    "content-type": "application/json",
    "access-control-allow-origin": "*",
  };
  if (status === 401) {
    headers["www-authenticate"] = `Bearer error="${category}"`;
  }
  if (extraHeaders) Object.assign(headers, extraHeaders);
  res.writeHead(status, headers);
  res.end(JSON.stringify({ error: { category, message } }));
};

// Terminal SSE `error` frame (Req 5.8): once dispatch has begun, a runtime
// failure or dropped upstream connection is surfaced in-band on the SSE stream
// rather than as an HTTP status, then the connection closes cleanly.
export const writeTerminalSseError = (res: any, message: string): void => {
  if (res.writableEnded) return;
  try {
    res.write(
      `event: error\ndata: ${JSON.stringify({ error: { category: "upstream_error", message } })}\n\n`,
    );
  } catch {
    /* best effort */
  }
  if (!res.writableEnded) res.end();
};

// Read the full request body off the stream (no global body parser is mounted;
// the internal routes read chunks the same way).
export const readRequestBody = (req: any): Promise<Buffer> =>
  new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });

// ---- Injected dependency contracts ----

export type ResolveCurrentPublishedAgentFn = (
  agentId: string,
) => Promise<Record<string, any> | null>;

export type VerifyAgentTokenFn = (
  token: string,
  integrationConfig: AgentIntegrationConfig,
) => Promise<VerifiedAgentToken>;

export type SignedRequestStreamFn = (
  url: string,
  body: Buffer,
  extraHeaders?: Record<string, string>,
) => Promise<IncomingMessage>;

export interface InvokeHandlerDeps {
  /** SigV4 invocations URL for the shared Task Agent runtime ("" when unset). */
  invokeTaskUrl: string;
  /** Whether the AssetIndex client is configured (false → 500 internal_error). */
  assetIndexReady: boolean;
  resolveCurrentPublishedAgent: ResolveCurrentPublishedAgentFn;
  verifyAgentToken: VerifyAgentTokenFn;
  signedRequestStream: SignedRequestStreamFn;
  /**
   * Optional per-agent / per-caller rate-limit & quota gate (task 14.1,
   * Req 14.1/14.2). When provided, the handler consumes one unit from both
   * scopes before dispatch and returns HTTP 429 (with `Retry-After`) when
   * either is exceeded. When omitted, no limiting is applied — preserving the
   * behavior isolated integration tests depend on.
   */
  rateLimiter?: InvokeRateLimiter;
  /**
   * Optional per-instance concurrent-SSE-stream cap (task 15.1, Req 15.4).
   * When provided, the handler reserves a slot immediately BEFORE dispatch and
   * returns HTTP 503 when the cap is already reached (never touching in-flight
   * streams); the slot is released exactly once on stream end/close/error.
   * When omitted, no cap is applied.
   */
  streamGuard?: StreamGuard;
  /**
   * Optional streaming transport tuning (task 15.1, Req 15.1/15.3): keepalive
   * cadence + the socket idle-timeout budget, pre-normalized to the ordering
   * invariant `keepalive < proxyIdle ≤ edgeIdle` and `runtime ≤ proxyIdle`.
   * Defaults to {@link DEFAULT_TRANSPORT_CONFIG} when omitted.
   */
  transport?: TransportConfig;
  /**
   * Optional emergency kill switch (task 17.1, Req 17.2/17.3/17.4). When
   * provided, it is checked FIRST — before rate-limit and before the agent is
   * resolved — and a global or per-agent stop rejects the request with HTTP 503
   * (audited as a denial). Admin-controlled and independent of Owner action.
   * When omitted, no kill switch is applied.
   */
  killSwitch?: KillSwitch;
  /**
   * Optional structured audit sink (task 17.1, Req 17.1). When provided, EVERY
   * external invocation — success or denial (401/403/404/429/503) — writes one
   * entry recording agentId, resolved published version, Owner, Caller `sub`,
   * and outcome. When omitted, no audit entries are emitted.
   */
  auditLogger?: AuditLogger;
}

/**
 * Build the `POST /invoke/:agentId` Express handler from injected deps. The
 * body is byte-for-byte the logic that previously lived inline in `app.ts`.
 */
export function createInvokeHandler(deps: InvokeHandlerDeps) {
  const {
    invokeTaskUrl,
    assetIndexReady,
    resolveCurrentPublishedAgent,
    verifyAgentToken,
    signedRequestStream,
    rateLimiter,
    streamGuard,
    transport = DEFAULT_TRANSPORT_CONFIG,
    killSwitch,
    auditLogger,
  } = deps;

  return async function invokeHandler(req: any, res: any): Promise<void> {
    const agentId = String(req.params?.agentId ?? "");

    // Audit context (task 17.1, Req 17.1): progressively filled as the request
    // resolves — agentId is always known; owner/version become known after
    // resolution, issuer after config load, callerSub after verification. It is
    // secret-free by construction (no token/header is ever stored here).
    const auditCtx: AuditContext = { agentId };

    // Emit exactly ONE audit entry per invocation, whatever the outcome. The
    // idempotent guard means the first terminal path to fire wins, so success,
    // denial, and stream-failure paths can all call this without double-logging.
    let auditEmitted = false;
    const emitAudit = (
      status: number,
      category: string,
      disposition?: AuditDisposition,
    ): void => {
      if (auditEmitted || !auditLogger) return;
      auditEmitted = true;
      auditLogger.record(
        buildAuditEntry(auditCtx, { status, category, disposition }),
      );
    };

    // Reject-with-audit: send an error-taxonomy response AND record the denial
    // in one place, so every pre-dispatch gate is traceable (Req 17.1). An
    // optional `auditCategory` lets a gate log a more specific category than the
    // HTTP taxonomy category it returns (e.g. kill-switch → 503 service_unavailable).
    const fail = (
      category: InvokeErrorCategory,
      message: string,
      opts?: { extraHeaders?: Record<string, string>; auditCategory?: string },
    ): void => {
      const status = invokeErrorStatus(category);
      emitAudit(status, opts?.auditCategory ?? category);
      sendInvokeError(res, category, message, opts?.extraHeaders);
    };

    try {
      // (Gate 0 — Req 17.2/17.3) KILL SWITCH, checked FIRST in the middleware
      // order: before rate-limit, before resolution, before the body is even
      // read. A global stop rejects every agent; a per-agent stop rejects one.
      // Admin-controlled and independent of Owner action (Req 17.4). Rejections
      // return HTTP 503 (service unavailable / emergency stop) and are audited.
      if (killSwitch) {
        const decision = killSwitch.evaluate(agentId);
        if (decision.blocked) {
          const globalStop = decision.scope === "global";
          fail(
            "service_unavailable",
            globalStop
              ? "External invocation is temporarily disabled platform-wide"
              : "External invocation is temporarily disabled for this agent",
            {
              extraHeaders: { "retry-after": "60" },
              auditCategory: globalStop ? "kill_switch_global" : "kill_switch_agent",
            },
          );
          return;
        }
      }

      // (Gate 1 — Req 14.1) RATE LIMIT / QUOTA, per-agent scope, hoisted to
      // step 2 of the fixed middleware order (design §Component 4): keyed on the
      // path `agentId` alone it needs NO verified principal, so it runs BEFORE
      // the expensive resolve (Req 16) and SSRF-hardened OIDC verify (Req 12/13)
      // steps to shed abusive per-agent load early. This is a NON-CONSUMING peek
      // — the authoritative, atomic consumption of BOTH the per-agent and the
      // per-caller scopes still happens in `rateLimiter.check(...)` after
      // verification (step 5b/6), where the per-caller key legitimately requires
      // the verified issuer-qualified principal. A per-agent rejection returns
      // HTTP 429 with a `Retry-After` hint and is audited as a denial (Req 17.1).
      if (rateLimiter) {
        const pre = rateLimiter.peekAgent(agentId);
        if (!pre.allowed) {
          const kind = pre.reason === "quota" ? "quota" : "rate limit";
          const retryAfter = Math.max(1, pre.retryAfterSec ?? 1);
          fail(
            "rate_limited",
            `The per-agent ${kind} for this agent has been exceeded; retry after ${retryAfter}s`,
            { extraHeaders: { "retry-after": String(retryAfter) } },
          );
          return;
        }
      }

      // Read the caller body up-front so the request stream is fully drained
      // before we begin the (async) resolve/verify/dispatch sequence.
      const rawBody = await readRequestBody(req);

      // Misconfiguration → 500 internal_error (fails closed; no dispatch).
      if (!assetIndexReady) {
        fail("internal_error", "Asset index is not configured");
        return;
      }
      if (!invokeTaskUrl) {
        fail("internal_error", "Task agent runtime is not configured");
        return;
      }

      // --- Fixed middleware order (task 19.1, design §Component 4) ---
      // Gate 0 KILL SWITCH (Req 17) ran FIRST above; Gate 1 the cheap per-agent
      // RATE-LIMIT peek (Req 14) ran at step 2 above (before resolution/verify).
      // Below, in order: (step 3) resolve + effective external-access gate
      // (Req 16), (step 4) SSRF-hardened OIDC verify (Req 12/13), (step 5) derive
      // the external session/memory key (Req 10) + the authoritative per-caller
      // rate-limit consumption (Req 14), (step 6) SigV4 dispatch with external
      // opt-in tools (Req 11), then (step 7) SSE stream w/ keepalive (Req 15) +
      // audit (Req 17).

      // (Step 1) Resolve current published version + integration_config (Req 5.2).
      const agentRow = await resolveCurrentPublishedAgent(agentId);
      // Record the resolved Owner + published version for audit as soon as the
      // row is known — so even a 404 (integration disabled / revoked) on an
      // existing agent still logs who owns it and which version was resolved.
      if (agentRow) {
        auditCtx.owner = agentRow.ownerUserId != null ? String(agentRow.ownerUserId) : null;
        auditCtx.publishedVersion = agentRow.version ?? null;
      }
      const integration = agentRow?.integrationConfig as
        | { enabled?: boolean; issuer?: string; audience?: string; allowedClientIds?: string[] }
        | undefined;

      // (Step 1 cont.) Effective external-access resolution (Req 16), computed
      // at REQUEST TIME so a revoke takes effect immediately and independently
      // of any publish (Req 16.1). The `externalAccessEnabled` gate lives on
      // the row (projected by the byPublishedId GSI) SEPARATELY from the
      // immutable snapshot's `integration_config`, and is written across the
      // whole lineage on revoke — so a rolled-back snapshot whose frozen
      // `integration_config.enabled` is true cannot silently re-grant access
      // the Owner revoked (Req 16.2, 16.3). Absent attribute → treated as
      // enabled (legacy rows predating the gate); only an explicit `false`
      // revokes. Effective access = snapshot.enabled AND gate-not-revoked.
      const externalAccessRevoked = agentRow?.externalAccessEnabled === false;

      // (Step 1 cont.) 404 for unknown agent, no current published version,
      // integration disabled, OR external access revoked (Req 5.3, 8.8, 4.5,
      // 16). Revocation folds into the same not_found taxonomy category as an
      // integration-disabled agent so a disable-then-invoke is rejected the
      // same way, without leaking whether the agent exists.
      if (
        !agentRow ||
        !integration ||
        integration.enabled !== true ||
        externalAccessRevoked
      ) {
        fail(
          "not_found",
          "No current published version with external integration enabled for this agent",
        );
        return;
      }

      const issuer = String(integration.issuer ?? "");
      const audience = String(integration.audience ?? "");
      auditCtx.issuer = issuer;
      const allowedClientIds = Array.isArray(integration.allowedClientIds)
        ? integration.allowedClientIds
        : [];

      // (Step 2) Extract the Bearer token; missing/malformed → 401 missing_token (Req 5.5).
      const authHeader = req.headers["authorization"];
      let token: string | null = null;
      if (typeof authHeader === "string" && authHeader.trim() !== "") {
        const match = /^Bearer\s+(.+)$/i.exec(authHeader.trim());
        if (match && match[1].trim()) {
          token = match[1].trim();
        }
      }
      if (!token) {
        fail("missing_token", "Authorization header must be 'Bearer <token>'");
        return;
      }

      // (Step 3) Verify signature/iss/aud/exp via the per-agent OIDC verifier
      // (task 10.1). SSRF-hardened discovery/JWKS fetch (Req 12) lives inside
      // that verifier. Typed failures map to their 401 taxonomy category;
      // anything else is an unexpected proxy error → 500 internal_error.
      let verified: { sub: string; clientId?: string | null };
      try {
        verified = await verifyAgentToken(token, {
          enabled: true,
          issuer,
          audience,
          allowedClientIds,
        });
      } catch (err: any) {
        if (err instanceof AgentTokenError) {
          fail(err.category, err.message);
          return;
        }
        console.error(
          `[agentcore-proxy] /invoke verify error (agent=${agentId}):`,
          err?.message ?? err,
        );
        fail("internal_error", "Token verification failed unexpectedly");
        return;
      }

      // (Step 4) Client-id gate (Req 5.6): when allowed_client_ids is set, the
      // token's client_id/azp must be a member → else 403 client_not_allowed.
      if (allowedClientIds.length > 0) {
        const clientId = verified.clientId ?? "";
        if (!clientId || !allowedClientIds.includes(clientId)) {
          fail(
            "client_not_allowed",
            "Token client_id/azp is not in the agent's allowed_client_ids",
          );
          return;
        }
      }

      // (Step 5) Subject required (Req 6.4): missing sub → 401 missing_subject.
      const sub = verified.sub;
      if (!sub || typeof sub !== "string") {
        fail("missing_subject", "Validated token has no subject (sub) claim");
        return;
      }
      // Caller subject is now verified — record it for audit (Req 17.1).
      auditCtx.callerSub = sub;

      // (Step 5b) Rate-limit / quota gate — AUTHORITATIVE consumption (task
      // 14.1/19.1, Req 14.1/14.2). Consume one unit atomically from BOTH the
      // per-agent (`agentId`) and per-caller (`agentId|<issuer>#<sub>`) scopes;
      // a rejection from either returns HTTP 429 with a `Retry-After` hint and
      // NEVER reaches dispatch. The per-caller key requires the verified issuer-
      // qualified Principal, so this consuming check sits after verification but
      // strictly before dispatch. The cheap per-agent pre-check hoisted to step 2
      // (Gate 1 above) only sheds load early and is NON-consuming, so budget is
      // charged exactly once here.
      if (rateLimiter) {
        const decision = rateLimiter.check(agentId, principalKey(issuer, sub));
        if (!decision.allowed) {
          const scope = decision.scope === "caller" ? "per-caller" : "per-agent";
          const kind = decision.reason === "quota" ? "quota" : "rate limit";
          const retryAfter = Math.max(1, decision.retryAfterSec ?? 1);
          fail(
            "rate_limited",
            `The ${scope} ${kind} for this agent has been exceeded; retry after ${retryAfter}s`,
            { extraHeaders: { "retry-after": String(retryAfter) } },
          );
          return;
        }
      }

      // (Step 6) Dispatch (Req 5.7, 6.1, 6.3, 6.6, 19.1). Stamp the TWO
      // identities. The Owner is the resource actor (the `actor_id` field
      // `task_agent.invoke()` reads — NOT `__actor_id`); the issuer-qualified
      // Principal is a SEPARATE field used only as End_User for memory/session/
      // audit and is never resolved as a resource owner.
      const ownerUserId = String(agentRow.ownerUserId ?? "");
      // Owner groups: the proxy has no owner-group source here (the JWT is the
      // Caller's, not the Owner's, and the index row does not store groups).
      // Stamp [] for now; resolving the Owner's groups is a follow-up. The
      // Task Agent's per-actor hook/authorization resolution keys on actor_id.
      const ownerGroups: string[] = [];

      // Parse the caller body, then STRIP any caller-supplied identity fields:
      // no caller-supplied input (body field or JWT) may override the stamped
      // resource actor or principal (Req 6.6, 19.1). Only the proxy — after
      // edge JWT verification — writes actor_id/actor_groups/principal/session_id.
      let callerBody: unknown = undefined;
      if (rawBody.length > 0) {
        try {
          callerBody = JSON.parse(rawBody.toString("utf8"));
        } catch {
          /* non-JSON caller body → treat as empty; prompt may be absent */
        }
      }

      // Optional per-conversation nonce (client-supplied) that sub-partitions
      // THIS caller's own memory so "new conversation" starts a fresh thread.
      // It only ever varies the key WITHIN the caller's (agentId, issuer, sub)
      // space — issuer/sub come from the verified token, so a caller can never
      // reach another caller's memory by choosing a conversation id. Sanitized
      // to a conservative charset (it feeds a hashed input, but keep it clean
      // and length-bounded defensively).
      let conversationId = "";
      if (callerBody && typeof callerBody === "object" && !Array.isArray(callerBody)) {
        const raw = (callerBody as Record<string, any>).conversation_id;
        if (typeof raw === "string") {
          conversationId = raw.replace(/[^A-Za-z0-9._-]+/g, "").slice(0, 64);
        }
      }

      // Canonical, collision-free external-keyspace session id derived purely
      // from (agentId, issuer, sub[, conversationId]) (task 12.3,
      // Req 10.1/19.1/19.3). The SAME value is stamped as the outbound
      // `session_id` body field (below, via stampInvocationIdentity →
      // ctx.sessionId) AND on the `x-amzn-bedrock-agentcore-runtime-session-id`
      // dispatch header, so the runtime session and the stamped principal agree.
      const sessionId = deriveExternalSessionId(agentId, issuer, sub, conversationId);

      // Build the invocation payload via the pure two-identity stamping seam
      // (Property 8, Req 6.1/6.2/6.3/6.6/19.1/19.2). The stamper strips any
      // caller-supplied identity fields, stamps the Owner as the resource actor
      // (`actor_id`) and the issuer-qualified Principal `(issuer, sub)` as a
      // separate `principal` field, preserves non-reserved caller fields, and
      // forwards the current published version's config context so the Task
      // Agent resolver resolves it in the Owner's context.
      const outbound = stampInvocationIdentity(callerBody, {
        agentId,
        ownerUserId,
        ownerGroups,
        issuer,
        sub,
        sessionId,
        conversationId,
        agentRow,
      });

      const outboundBody = Buffer.from(JSON.stringify(outbound), "utf8");

      // (Step 5c) Concurrency cap (task 15.1, Req 15.4). SSE responses are
      // long-lived; bound the number of in-flight streams per proxy instance so
      // a burst can't exhaust sockets/memory and degrade streams already
      // flowing. When at capacity, reject with HTTP 503 BEFORE dispatch. The
      // reserved slot is released exactly once below on end/close/error. NOTE:
      // this is per-instance state — behind an ALB with N tasks the effective
      // cap is ~N× the configured value (documented tradeoff; a distributed cap
      // is a future enhancement, mirroring the rate limiter).
      let streamSlotHeld = false;
      if (streamGuard) {
        if (!streamGuard.tryAcquire()) {
          fail(
            "service_unavailable",
            "The proxy is at its concurrent stream capacity; retry shortly",
            {
              extraHeaders: { "retry-after": "1" },
              auditCategory: "stream_capacity",
            },
          );
          return;
        }
        streamSlotHeld = true;
      }
      // Release the concurrency slot exactly once (idempotent) — wired to every
      // terminal path below so the counter can't leak and wedge the cap open.
      const releaseStreamSlot = () => {
        if (streamSlotHeld) {
          streamSlotHeld = false;
          streamGuard?.release();
        }
      };

      // SigV4-dispatch to the SHARED Task Agent runtime and stream SSE back,
      // reusing the same signed-streaming path as the internal /api route.
      let upstream: IncomingMessage;
      try {
        upstream = await signedRequestStream(invokeTaskUrl, outboundBody, {
          "x-amzn-bedrock-agentcore-runtime-session-id": sessionId,
        });
      } catch (dispatchErr: any) {
        // Failed to establish the upstream stream (Req 5.8). Open the SSE
        // response and emit a terminal error frame rather than a bare 502.
        releaseStreamSlot();
        console.error(
          `[agentcore-proxy] /invoke dispatch error (agent=${agentId}):`,
          dispatchErr?.message ?? dispatchErr,
        );
        if (!res.headersSent) {
          res.writeHead(200, {
            "content-type": "text/event-stream",
            "access-control-allow-origin": "*",
            "cache-control": "no-cache",
            connection: "keep-alive",
            // Disable proxy buffering (Req 15.2): nginx/ingress honors this so
            // frames are not batched. NOTE (infra): the CloudFront/ALB config
            // for the `/invoke` path must ALSO bypass response buffering (e.g. a
            // dedicated cache policy / origin-response streaming) — that edge
            // config is outside the app, but the app sets every header it owns.
            "x-accel-buffering": "no",
          });
        }
        // The SSE stream was opened (HTTP 200) but dispatch failed — audit as an
        // error outcome (synthetic 502 upstream) rather than a clean success.
        emitAudit(502, "dispatch_failed", "error");
        writeTerminalSseError(res, "Failed to reach the task agent runtime");
        return;
      }

      const upstreamStatus = upstream.statusCode ?? 200;
      res.writeHead(upstreamStatus, {
        "content-type": (upstream.headers["content-type"] as string) ?? "text/event-stream",
        "access-control-allow-origin": "*",
        "cache-control": "no-cache",
        connection: "keep-alive",
        // Disable proxy buffering so frames flush to the caller promptly
        // (Req 15.2). See the infra note above re: CloudFront/ALB `/invoke`.
        "x-accel-buffering": "no",
      });
      // Immediate flush (Req 15.2): push headers to the caller now so the SSE
      // stream is established before the first frame, and don't sit in a
      // platform header buffer waiting on the first body write.
      if (typeof res.flushHeaders === "function") res.flushHeaders();

      // Align socket idle timeouts (Req 15.3): apply the proxy idle timeout so
      // the ordering edge ≥ proxy ≥ runtime holds (see streamGuard.ts). With
      // keepalives writing every `keepaliveMs`, this only fires on a genuinely
      // dead connection; 0 disables it. Setting it on both the request and
      // response sockets covers inbound-idle and outbound-idle alike.
      if (transport.proxyIdleTimeoutMs > 0) {
        try {
          req.setTimeout?.(transport.proxyIdleTimeoutMs);
          res.setTimeout?.(transport.proxyIdleTimeoutMs);
        } catch {
          /* setTimeout unsupported on the platform response — best effort */
        }
      }

      // Keepalive (Req 15.1): while the SSE response is open, emit a periodic
      // `: keep-alive` SSE comment during IDLE gaps (e.g. long model-thinking)
      // to keep the connection within the edge/LB idle-timeout window. Emitting
      // ONLY when the stream has been idle for a full interval avoids splicing a
      // comment into the middle of a real frame, so keepalives never corrupt
      // upstream data. SSE comment lines (leading `:`) are ignored by clients.
      let lastActivity = Date.now();
      let keepaliveTimer: ReturnType<typeof setInterval> | null = null;
      const stopKeepalive = () => {
        if (keepaliveTimer) {
          clearInterval(keepaliveTimer);
          keepaliveTimer = null;
        }
      };
      if (transport.keepaliveMs > 0) {
        keepaliveTimer = setInterval(() => {
          if (res.writableEnded) {
            stopKeepalive();
            return;
          }
          if (Date.now() - lastActivity < transport.keepaliveMs) return;
          try {
            res.write(": keep-alive\n\n");
            if (typeof res.flush === "function") res.flush();
            lastActivity = Date.now();
          } catch {
            /* best effort — a failed write means the stream is tearing down */
          }
        }, transport.keepaliveMs);
        // Don't let the heartbeat keep the event loop alive on shutdown.
        if (typeof keepaliveTimer.unref === "function") keepaliveTimer.unref();
      }

      // Pass SSE frames through live; stop if the caller disconnects. Every
      // terminal path stops the keepalive and releases the concurrency slot.
      let clientGone = false;
      const onClientClose = () => {
        clientGone = true;
        stopKeepalive();
        releaseStreamSlot();
        // Caller hung up mid-stream — audit as an (incomplete) outcome. Idempotent
        // with the end/error paths, so whichever fires first wins.
        emitAudit(499, "client_closed", "error");
        upstream.destroy();
      };
      req.on("close", onClientClose);
      upstream.on("data", (chunk: Buffer) => {
        if (!clientGone && !res.writableEnded) {
          res.write(chunk);
          // Mark activity so an idle keepalive isn't spliced into live frames.
          lastActivity = Date.now();
          if (typeof res.flush === "function") res.flush();
        }
      });
      upstream.on("end", () => {
        req.off("close", onClientClose);
        stopKeepalive();
        releaseStreamSlot();
        // Stream completed — the dispatched invocation's terminal outcome. Audit
        // with the upstream status (success taxonomy category on 2xx) (Req 17.1).
        emitAudit(
          upstreamStatus,
          upstreamStatus >= 200 && upstreamStatus < 300 ? "success" : "upstream_status",
        );
        if (!res.writableEnded) res.end();
      });
      upstream.on("error", (streamErr: Error) => {
        req.off("close", onClientClose);
        stopKeepalive();
        releaseStreamSlot();
        console.error("[agentcore-proxy] /invoke upstream stream error:", streamErr.message);
        // Terminal SSE error on stream failure (Req 5.8); audit as an error
        // outcome (synthetic 502) since the HTTP status was already 200.
        emitAudit(502, "stream_failed", "error");
        writeTerminalSseError(res, "Task agent stream failed");
      });
    } catch (err: any) {
      // Any unexpected error BEFORE dispatch → 500 internal_error.
      console.error(
        `[agentcore-proxy] /invoke internal error (agent=${agentId}):`,
        err?.message ?? err,
      );
      if (!res.headersSent) {
        fail("internal_error", "Unexpected proxy error");
      } else {
        // Headers already sent (mid-stream) — can't send an HTTP error; still
        // audit the failure before closing.
        emitAudit(500, "internal_error", "error");
        if (!res.writableEnded) res.end();
      }
    }
  };
}
