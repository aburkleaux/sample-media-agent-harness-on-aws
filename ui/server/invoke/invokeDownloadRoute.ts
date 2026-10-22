/**
 * External artifact download route — `GET /invoke/:agentId/download?ref=<s3 uri|key>`
 * (stable download links for the external OIDC caller realm).
 *
 * The external twin of the Cognito-gated `/download` in `app.ts`. Both exist for
 * the same reason: a presigned S3 URL is ~1,700 characters of query-string, and
 * an agent that pastes a handful of them into one reply exhausts its output
 * budget and truncates mid-URL — the caller then sees a successful job with no
 * usable links at all. A `?ref=` link to this route is ~196 characters, so the
 * agent can hand back a dozen without approaching the cap.
 *
 * `app.ts`'s `/download` lives in `authPaths`, so it is reachable only with a
 * platform Cognito token. External callers authenticate against the agent's OWN
 * IdP, so they need this sibling on the `/invoke/:agentId/*` OIDC realm. The
 * identity gate below is deliberately identical (and in the same order) to
 * `invokeManifestRoute` / `invokeJobsRoute`: resolve the current published
 * version + effective external-access → per-agent OIDC verify → client-id and
 * subject gates.
 *
 * Like the Cognito route, this **302-redirects to a freshly minted presigned
 * URL** rather than streaming bytes: the object never transits the single
 * `desiredCount: 1` Fargate task, and each request mints a new credential so a
 * link that is bookmarked and reused keeps working without the proxy holding
 * long-lived state.
 *
 * SECURITY: the caller may read only their OWN work (externalDownloadScope.ts) — artifacts under
 * their derived session or a shared namespace, and M2C outputs of jobs that session ran. This
 * replaced a bucket-only allowlist under which any verified caller of any enabled agent could
 * presign ANY key in either bucket for 12 hours. Traversal is still rejected outright.
 *
 * Registered OUTSIDE `authPaths` (alongside the other `/invoke/:agentId/*`
 * routes) so it does NOT hit the internal Cognito `authMiddleware`.
 */

import {
  AgentTokenError,
  type AgentIntegrationConfig,
  type VerifiedAgentToken,
} from "../auth/agentOidc";
import {
  sendInvokeError,
  type ResolveCurrentPublishedAgentFn,
  type VerifyAgentTokenFn,
} from "./invokeRoute";
import { deriveExternalSessionId } from "./externalSessionKey";
import { externalArtifactOwnerTag } from "../artifactOwner";
import {
  authorizeExternalRef,
  sanitizeConversationId,
  type ExternalScope,
} from "./externalDownloadScope";

// ---- Injected dependency contracts ----

/**
 * Mint a presigned GET URL for `bucket`/`key`. When `attach` is true the URL
 * carries a `attachment` content-disposition so browsers save rather than play.
 * Injected so the handler is testable without a live S3.
 */
export type PresignGetFn = (
  bucket: string,
  key: string,
  attach: boolean,
) => Promise<string>;

export interface InvokeDownloadHandlerDeps {
  /** Whether the AssetIndex client is configured (false → 500 internal_error). */
  assetIndexReady: boolean;
  /** Buckets a caller may read from. A ref outside these → 400. */
  allowedBuckets: string[];
  /** Bucket a bare (non-`s3://`) key is resolved against. */
  defaultBucket: string;
  /** The M2C output bucket ("" when unset): its objects are readable only as the caller's job outputs. */
  mediaBucket?: string;
  resolveCurrentPublishedAgent: ResolveCurrentPublishedAgentFn;
  verifyAgentToken: VerifyAgentTokenFn;
  presignGet: PresignGetFn;
  /** The Owner's recent job rows (newest first), used to recognise the caller's own M2C outputs. */
  queryActorJobs?: (actorId: string, limit: number) => Promise<Record<string, any>[]>;
}

/** Max job rows read when matching an M2C output to the caller (same cap as the jobs route). */
const ACTOR_JOBS_SCAN_LIMIT = 200;

/** A parsed, validated artifact reference. */
export interface ParsedRef {
  bucket: string;
  key: string;
}

/**
 * Parse and validate a caller-supplied `ref`.
 *
 * Accepts either a full `s3://bucket/key` URI or a bare key (resolved against
 * `defaultBucket`, matching the Cognito `/download` contract). Returns null for
 * anything malformed or traversal-bearing — the caller maps that to a 400.
 *
 * Rejects `..` and backslashes explicitly. Unlike the manifest route there is no
 * key-shape regex to fall back on (a media output can be any key), so these
 * checks plus the bucket allowlist in the handler are the entire guard.
 */
export function parseArtifactRef(
  raw: unknown,
  defaultBucket: string,
): ParsedRef | null {
  if (typeof raw !== "string") return null;
  const ref = raw.trim();
  if (!ref) return null;
  // Traversal / escaping never has a legitimate use in an artifact ref.
  if (ref.includes("..") || ref.includes("\\")) return null;

  let bucket: string;
  let key: string;
  if (ref.startsWith("s3://")) {
    const rest = ref.slice("s3://".length);
    const slash = rest.indexOf("/");
    if (slash <= 0) return null;
    bucket = rest.slice(0, slash);
    key = rest.slice(slash + 1);
  } else {
    // A bare key belongs to this app's own artifacts/upload bucket. Reject an
    // absolute-looking key rather than silently stripping the leading slash, so
    // a caller can't smuggle a different shape past the allowlist.
    if (ref.startsWith("/")) return null;
    bucket = defaultBucket;
    key = ref;
  }

  if (!bucket || !key) return null;
  // A prefix is not a downloadable object.
  if (key.endsWith("/")) return null;
  return { bucket, key };
}

/**
 * Refuse a ref. 400, NOT 403/404: the UI CloudFront distribution rewrites 403 and 404 to a
 * `200 index.html` for every behaviour, so a 403 reached a browser caller as the SPA's HTML, which
 * the external console then tried to open as a download. 400 survives the error map.
 */
function sendRefused(res: any, category: string, message: string): void {
  res.writeHead(400, {
    "content-type": "application/json",
    "access-control-allow-origin": "*",
  });
  res.end(JSON.stringify({ error: { category, message } }));
}

/**
 * Build the `GET /invoke/:agentId/download` handler.
 */
export function createInvokeDownloadHandler(deps: InvokeDownloadHandlerDeps) {
  const {
    assetIndexReady,
    allowedBuckets,
    defaultBucket,
    mediaBucket = "",
    resolveCurrentPublishedAgent,
    verifyAgentToken,
    presignGet,
    queryActorJobs,
  } = deps;

  const allowed = new Set(allowedBuckets.filter(Boolean));

  return async function invokeDownloadHandler(req: any, res: any): Promise<void> {
    const agentId = String(req.params?.agentId ?? "");

    try {
      if (!assetIndexReady) {
        sendInvokeError(res, "internal_error", "Asset index is not configured");
        return;
      }
      if (allowed.size === 0) {
        sendInvokeError(res, "internal_error", "No artifact buckets configured");
        return;
      }

      // (Step 1) Resolve current published version + effective external-access
      // gate. 404 never leaks whether the agent exists.
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

      // (Step 2) Bearer token.
      const authHeader = req.headers["authorization"];
      let token: string | null = null;
      if (typeof authHeader === "string" && authHeader.trim() !== "") {
        const match = /^Bearer\s+(.+)$/i.exec(authHeader.trim());
        if (match && match[1].trim()) token = match[1].trim();
      }
      if (!token) {
        sendInvokeError(
          res,
          "missing_token",
          "Authorization header must be 'Bearer <token>'",
        );
        return;
      }

      // (Step 3) Per-agent OIDC verify.
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
          `[agentcore-proxy] /invoke/:agentId/download verify error (agent=${agentId}):`,
          err?.message ?? err,
        );
        sendInvokeError(
          res,
          "internal_error",
          "Token verification failed unexpectedly",
        );
        return;
      }

      // (Step 4) Client-id gate.
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

      // (Step 5) Subject required.
      if (!verified.sub || typeof verified.sub !== "string") {
        sendInvokeError(
          res,
          "missing_subject",
          "Validated token has no subject (sub) claim",
        );
        return;
      }

      // Caller authorized. Parse + allowlist the ref BEFORE any S3 access.
      const urlObj = new URL(req.url || "/", "http://localhost");
      const parsed = parseArtifactRef(urlObj.searchParams.get("ref"), defaultBucket);
      if (!parsed) {
        res.writeHead(400, {
          "content-type": "application/json",
          "access-control-allow-origin": "*",
        });
        res.end(
          JSON.stringify({
            error: {
              category: "invalid_ref",
              message:
                "ref must be an s3://bucket/key URI or a bare object key (no traversal)",
            },
          }),
        );
        return;
      }
      if (!allowed.has(parsed.bucket)) {
        sendRefused(res, "bucket_not_allowed", "bucket not allowed");
        return;
      }

      // (Step 6) Per-caller scope. The caller may read only their OWN work: artifacts under their
      // derived session (or a shared namespace), and M2C outputs of jobs that session ran. The
      // session folds in the optional conversation nonce exactly as invoke and /jobs do, so a link
      // from one conversation does not open another's — and never another caller's.
      const scope: ExternalScope = {
        sessionId: deriveExternalSessionId(
          agentId,
          issuer,
          verified.sub,
          sanitizeConversationId(urlObj.searchParams.get("conversation_id")),
        ),
        ownerTag: externalArtifactOwnerTag(agentId, issuer, verified.sub),
        ownerActorId: String(agentRow.ownerUserId ?? ""),
        uploadBucket: defaultBucket,
        mediaBucket,
      };
      const decision = await authorizeExternalRef(parsed, scope, async () =>
        queryActorJobs && scope.ownerActorId ? queryActorJobs(scope.ownerActorId, ACTOR_JOBS_SCAN_LIMIT) : [],
      );
      if (!decision.ok) {
        // One answer for every refusal reason, so a caller cannot probe whether another
        // session's or caller's object exists.
        sendRefused(res, "artifact_not_readable", "This artifact is not part of your conversation");
        return;
      }

      const attach = urlObj.searchParams.get("attach") === "1";
      const signed = await presignGet(parsed.bucket, parsed.key, attach);

      // A browser client on another origin cannot follow the 302: it lands on
      // S3, which sends no CORS headers, so `fetch` rejects with an opaque
      // "Failed to fetch" even though the redirect was valid. `Accept:
      // application/json` returns the URL as data instead, keeping the
      // cross-origin read on this API (which does send CORS headers); the caller
      // then hands the URL to the browser, which is not CORS-bound.
      const wantsJson = /application\/json/i.test(
        String(req.headers?.["accept"] ?? ""),
      );
      if (wantsJson) {
        res.writeHead(200, {
          "content-type": "application/json",
          "cache-control": "no-store",
          "access-control-allow-origin": "*",
        });
        res.end(JSON.stringify({ url: signed }));
        return;
      }

      // 302 to the presigned URL: bytes never transit the proxy task, and each
      // request mints a fresh credential so the short link keeps working.
      // Kept as the default so `curl -L` and plain navigation still work.
      res.writeHead(302, {
        location: signed,
        "cache-control": "no-store",
        "access-control-allow-origin": "*",
      });
      res.end();
    } catch (err: any) {
      console.error(
        `[agentcore-proxy] /invoke/:agentId/download error (agent=${agentId}):`,
        err?.message ?? err,
      );
      if (!res.headersSent) {
        sendInvokeError(res, "internal_error", "Failed to build download link");
      } else if (!res.writableEnded) {
        res.end();
      }
    }
  };
}
