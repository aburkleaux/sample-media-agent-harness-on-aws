/**
 * External jobs read route — `GET /invoke/:agentId/jobs` (long-running workflow
 * tracking for the external OIDC caller realm).
 *
 * The external console (bring-your-own-OIDC) drives a published Custom Agent via
 * `POST /invoke/:agentId`. When the agent dispatches a long media job it returns
 * a job id and the invoke stream ends — the caller then has no way to watch the
 * job to completion. The dev portal reads the shared DynamoDB JobRegistry via
 * `GET /jobs*`, but those routes are gated to the internal Cognito operator
 * identity and are unreachable by the OIDC caller.
 *
 * This route closes that gap on the SAME OIDC realm as `/invoke`. It reuses the
 * identical identity gate (resolve current published version + effective
 * external-access → per-agent OIDC verify → client-id/subject gates) and then
 * returns ONLY the caller's own jobs, scoped to BOTH:
 *   - `actorId === ownerUserId`   (the Owner is the resource actor jobs are
 *                                  stamped under), AND
 *   - `sessionId === deriveExternalSessionId(agentId, issuer, sub)`
 *                                  (the caller's derived external session).
 * A job belonging to another caller (same Owner, different derived session) or
 * to the internal operator is never returned or leaked.
 *
 * Registered OUTSIDE `authPaths` (alongside `POST /invoke/:agentId`) so it does
 * NOT hit the internal Cognito `authMiddleware`; auth is the per-agent OIDC
 * verify performed inside this handler.
 */

import {
  AgentTokenError,
  type AgentIntegrationConfig,
  type VerifiedAgentToken,
} from "../auth/agentOidc";
import { deriveExternalSessionId } from "./externalSessionKey";
import { projectJob } from "../jobsProjection";
import { parseSfnJobOutput, type JobOutput } from "./sfnJobOutput";
import {
  sendInvokeError,
  type ResolveCurrentPublishedAgentFn,
  type VerifyAgentTokenFn,
} from "./invokeRoute";

// ---- Injected dependency contracts ----

/**
 * Query the JobRegistry for an actor's jobs, newest-first, via the
 * `actor-status-index` GSI. Returns raw DDB items (unprojected). Injected so the
 * handler can be integration-tested without a live DynamoDB.
 */
export type QueryActorJobsFn = (
  actorId: string,
  limit: number,
) => Promise<Record<string, any>[]>;

/**
 * Fetch a Step Functions execution's `{ status, output }` by ARN. Injected so
 * the handler can read a completed job's artifacts DIRECTLY from SFN — no agent
 * turn. Returns null when the execution can't be described (missing / not SFN /
 * error) so enrichment is best-effort and never blocks the status list.
 */
export type DescribeExecutionFn = (
  executionArn: string,
) => Promise<{ status?: string; output?: string } | null>;

/** The SFN-gateway marker on a JobRegistry row whose jobId is an execution ARN. */
export const SFN_GATEWAY_ID = "__aws_sfn__";

export interface InvokeJobsHandlerDeps {
  /** Whether the AssetIndex client is configured (false → 500 internal_error). */
  assetIndexReady: boolean;
  /** Whether the JobRegistry client/table is configured (false → 500). */
  jobRegistryReady: boolean;
  resolveCurrentPublishedAgent: ResolveCurrentPublishedAgentFn;
  verifyAgentToken: VerifyAgentTokenFn;
  queryActorJobs: QueryActorJobsFn;
  /**
   * Optional Step Functions describe seam. When provided, SUCCEEDED SFN jobs
   * (gatewayId === "__aws_sfn__") are enriched with their parsed output
   * artifacts read straight from SFN. When omitted, jobs are returned without
   * an `output` field (status-only) — the console still shows running→done.
   */
  describeExecution?: DescribeExecutionFn;
}

/** Max rows scanned from the actor GSI before session-filtering (safety cap). */
const ACTOR_JOBS_SCAN_LIMIT = 200;

/** A projected job optionally enriched with its SFN outputs. */
type JobWithOutput = ReturnType<typeof projectJob> & { output?: JobOutput };

/**
 * Build the `GET /invoke/:agentId/jobs` handler from injected deps. The identity
 * gate is deliberately identical (in the same order) to `createInvokeHandler`'s
 * steps 1–5, minus dispatch — so the read realm and the invoke realm authorize a
 * caller the exact same way.
 */
export function createInvokeJobsHandler(deps: InvokeJobsHandlerDeps) {
  const {
    assetIndexReady,
    jobRegistryReady,
    resolveCurrentPublishedAgent,
    verifyAgentToken,
    queryActorJobs,
    describeExecution,
  } = deps;

  return async function invokeJobsHandler(req: any, res: any): Promise<void> {
    const agentId = String(req.params?.agentId ?? "");

    try {
      // Misconfiguration → 500 (fails closed; no read).
      if (!assetIndexReady) {
        sendInvokeError(res, "internal_error", "Asset index is not configured");
        return;
      }
      if (!jobRegistryReady) {
        sendInvokeError(res, "internal_error", "Job registry is not configured");
        return;
      }

      // (Step 1) Resolve current published version + integration_config, then
      // apply the effective external-access gate — identical to invoke. A
      // 404 (unknown / no current published version / integration disabled /
      // external access revoked) never leaks whether the agent exists.
      const agentRow = await resolveCurrentPublishedAgent(agentId);
      const integration = agentRow?.integrationConfig as
        | {
            enabled?: boolean;
            issuer?: string;
            audience?: string;
            allowedClientIds?: string[];
          }
        | undefined;
      const externalAccessRevoked = agentRow?.externalAccessEnabled === false;
      if (
        !agentRow ||
        !integration ||
        integration.enabled !== true ||
        externalAccessRevoked
      ) {
        sendInvokeError(
          res,
          "not_found",
          "No current published version with external integration enabled for this agent",
        );
        return;
      }

      const issuer = String(integration.issuer ?? "");
      const audience = String(integration.audience ?? "");
      const allowedClientIds = Array.isArray(integration.allowedClientIds)
        ? integration.allowedClientIds
        : [];

      // (Step 2) Extract the Bearer token; missing/malformed → 401 missing_token.
      const authHeader = req.headers["authorization"];
      let token: string | null = null;
      if (typeof authHeader === "string" && authHeader.trim() !== "") {
        const match = /^Bearer\s+(.+)$/i.exec(authHeader.trim());
        if (match && match[1].trim()) {
          token = match[1].trim();
        }
      }
      if (!token) {
        sendInvokeError(
          res,
          "missing_token",
          "Authorization header must be 'Bearer <token>'",
        );
        return;
      }

      // (Step 3) Verify signature/iss/aud/exp via the per-agent OIDC verifier.
      const integrationConfig: AgentIntegrationConfig = {
        enabled: true,
        issuer,
        audience,
        allowedClientIds,
      };
      let verified: VerifiedAgentToken;
      try {
        verified = await verifyAgentToken(token, integrationConfig);
      } catch (err: any) {
        if (err instanceof AgentTokenError) {
          sendInvokeError(res, err.category, err.message);
          return;
        }
        console.error(
          `[agentcore-proxy] /invoke/:agentId/jobs verify error (agent=${agentId}):`,
          err?.message ?? err,
        );
        sendInvokeError(
          res,
          "internal_error",
          "Token verification failed unexpectedly",
        );
        return;
      }

      // (Step 4) Client-id gate: when allowed_client_ids is set, the token's
      // client_id/azp must be a member → else 403 client_not_allowed.
      if (allowedClientIds.length > 0) {
        const clientId = verified.clientId ?? "";
        if (!clientId || !allowedClientIds.includes(clientId)) {
          sendInvokeError(
            res,
            "client_not_allowed",
            "Token client_id/azp is not in the agent's allowed_client_ids",
          );
          return;
        }
      }

      // (Step 5) Subject required → 401 missing_subject.
      const sub = verified.sub;
      if (!sub || typeof sub !== "string") {
        sendInvokeError(
          res,
          "missing_subject",
          "Validated token has no subject (sub) claim",
        );
        return;
      }

      // Caller is authorized. Scope reads to (Owner actorId, derived session).
      // The session must match the one jobs were DISPATCHED under, which folds
      // in the same optional conversation nonce (query param, sanitized). So the
      // panel shows the current conversation's jobs; a caller still cannot see
      // another caller's jobs (issuer/sub come from the verified token).
      const ownerUserId = String(agentRow.ownerUserId ?? "");
      const urlObj = new URL(req.url || "/", "http://localhost");
      const conversationId = (urlObj.searchParams.get("conversation_id") || "")
        .replace(/[^A-Za-z0-9._-]+/g, "")
        .slice(0, 64);
      const sessionId = deriveExternalSessionId(agentId, issuer, sub, conversationId);

      // Query the Owner's jobs newest-first, then filter to THIS caller's derived
      // session so a caller only ever sees their own jobs (never the operator's
      // or another external caller's). No session GSI exists; the per-actor job
      // volume is small, so an in-code filter after the actor query is sufficient.
      const items = await queryActorJobs(ownerUserId, ACTOR_JOBS_SCAN_LIMIT);
      const mine = items.filter(
        (it) => it.actorId === ownerUserId && it.sessionId === sessionId,
      );

      // Enrich SUCCEEDED Step Functions jobs with their output artifacts read
      // DIRECTLY from SFN — no agent turn, no tokens. Best-effort: a describe
      // failure just omits `output` for that job. Runs in parallel across jobs.
      const jobs: JobWithOutput[] = await Promise.all(
        mine.map(async (it): Promise<JobWithOutput> => {
          const job = projectJob(it) as JobWithOutput;
          if (
            describeExecution &&
            job.status === "SUCCEEDED" &&
            it.gatewayId === SFN_GATEWAY_ID &&
            job.jobId.startsWith("arn:aws:states:")
          ) {
            try {
              const desc = await describeExecution(job.jobId);
              if (desc?.output) job.output = parseSfnJobOutput(desc.output);
            } catch {
              /* best-effort — leave output undefined */
            }
          }
          return job;
        }),
      );

      res.writeHead(200, {
        "content-type": "application/json",
        "access-control-allow-origin": "*",
      });
      res.end(JSON.stringify({ jobs }));
    } catch (err: any) {
      console.error(
        `[agentcore-proxy] /invoke/:agentId/jobs error (agent=${agentId}):`,
        err?.message ?? err,
      );
      sendInvokeError(res, "internal_error", "Failed to read jobs");
    }
  };
}
