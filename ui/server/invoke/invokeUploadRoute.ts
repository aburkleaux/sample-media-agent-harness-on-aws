/**
 * External media upload route — `POST /invoke/:agentId/uploads`
 * (presigned-PUT issuance for the external OIDC caller realm).
 *
 * The external console (bring-your-own-OIDC) drives a published Custom Agent via
 * `POST /invoke/:agentId`, but had no way to get its own media INTO the platform
 * — the sample asset was hard-coded. This route lets an external caller upload a
 * media file to the SAME `artifacts/` location the agent reads from, without the
 * bytes ever transiting the proxy: it verifies the caller with the identical
 * per-agent OIDC gate as `/invoke`, then returns a short-lived presigned S3 PUT
 * URL. The browser PUTs the file directly to S3, then references the returned
 * `s3Uri` in its next prompt.
 *
 * Registered OUTSIDE `authPaths` (alongside the other `/invoke/:agentId/*`
 * routes) so it does NOT hit the internal Cognito `authMiddleware`; auth is the
 * per-agent OIDC verify performed inside this handler.
 */

import {
  AgentTokenError,
  type AgentIntegrationConfig,
  type VerifiedAgentToken,
} from "../auth/agentOidc";
import {
  readRequestBody,
  sendInvokeError,
  type ResolveCurrentPublishedAgentFn,
  type VerifyAgentTokenFn,
} from "./invokeRoute";
import { uploadKeyFor } from "../uploadKey";
import { externalArtifactOwnerTag } from "../artifactOwner";

// ---- Injected dependency contracts ----

/**
 * Produce a presigned S3 PUT URL for an object key. Injected so the handler can
 * be integration-tested without AWS. Returns the URL a browser can `PUT` bytes
 * to directly.
 */
export type PresignPutFn = (
  key: string,
  contentType: string,
) => Promise<string>;

export interface InvokeUploadHandlerDeps {
  /** Whether the AssetIndex client is configured (false → 500 internal_error). */
  assetIndexReady: boolean;
  /** Whether the S3 upload bucket is configured (false → 500). */
  uploadReady: boolean;
  /** The bucket presigned PUTs target (for building the returned s3:// URI). */
  uploadBucket: string;
  resolveCurrentPublishedAgent: ResolveCurrentPublishedAgentFn;
  verifyAgentToken: VerifyAgentTokenFn;
  presignPut: PresignPutFn;
  /** Wall-clock ms, injected so tests are deterministic (defaults to Date.now). */
  now?: () => number;
}

/** Presigned PUT lifetime — long enough for a large upload, short enough to leak little. */
export const UPLOAD_URL_TTL_SECONDS = 900; // 15 min

/** Max filename length accepted (defensive; keeps keys sane). */
const MAX_FILENAME_LEN = 200;

/**
 * Sanitize a caller-supplied filename into a safe, single-segment object-name
 * suffix. Strips any path components and anything outside a conservative
 * allowlist so the caller can never traverse out of the `artifacts/` prefix or
 * inject control characters into the key.
 */
export function sanitizeFilename(raw: unknown): string {
  let name = typeof raw === "string" ? raw : "";
  // Drop any directory components (both separators) — keep the basename only.
  name = name.split(/[/\\]/).pop() ?? "";
  // Collapse to an allowlist: letters, digits, dot, dash, underscore.
  name = name.replace(/[^A-Za-z0-9._-]+/g, "_");
  // No leading dots (hidden files / "..").
  name = name.replace(/^\.+/, "");
  if (name.length > MAX_FILENAME_LEN) {
    // Preserve the extension when truncating.
    const dot = name.lastIndexOf(".");
    const ext = dot > 0 ? name.slice(dot) : "";
    name = name.slice(0, MAX_FILENAME_LEN - ext.length) + ext;
  }
  return name || "upload.bin";
}

/**
 * Build the `POST /invoke/:agentId/uploads` handler. The identity gate is
 * identical (and in the same order) to `createInvokeHandler` / the jobs route:
 * resolve current published version + effective external-access → per-agent OIDC
 * verify → client-id/subject gates. On success it returns a presigned PUT URL
 * + the canonical `s3://` URI the caller then feeds into its prompt.
 *
 * Request body (JSON): `{ filename: string, contentType?: string }`.
 * Response (200): `{ putUrl, s3Uri, key, bucket, expiresIn }`.
 */
export function createInvokeUploadHandler(deps: InvokeUploadHandlerDeps) {
  const {
    assetIndexReady,
    uploadReady,
    uploadBucket,
    resolveCurrentPublishedAgent,
    verifyAgentToken,
    presignPut,
    now = () => Date.now(),
  } = deps;

  return async function invokeUploadHandler(req: any, res: any): Promise<void> {
    const agentId = String(req.params?.agentId ?? "");

    try {
      // Misconfiguration → 500 (fails closed; no presign).
      if (!assetIndexReady) {
        sendInvokeError(res, "internal_error", "Asset index is not configured");
        return;
      }
      if (!uploadReady || !uploadBucket) {
        sendInvokeError(res, "internal_error", "Upload bucket is not configured");
        return;
      }

      // Read the body up-front so the request stream is drained before verify.
      const rawBody = await readRequestBody(req);

      // (Step 1) Resolve current published version + effective external-access
      // gate — identical to invoke. 404 never leaks whether the agent exists.
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
          `[agentcore-proxy] /invoke/:agentId/uploads verify error (agent=${agentId}):`,
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

      // Caller authorized. Parse the upload request body.
      let body: any = {};
      if (rawBody.length > 0) {
        try {
          body = JSON.parse(rawBody.toString("utf8"));
        } catch {
          sendInvokeError(res, "invalid_token", "Request body must be JSON");
          return;
        }
      }
      const filename = sanitizeFilename(body?.filename);
      const contentType =
        typeof body?.contentType === "string" && body.contentType
          ? body.contentType
          : "application/octet-stream";

      // Write into the SAME artifacts/ location the agent reads from, using the SAME
      // placement rule as the dev-portal upload (`uploadKeyFor`) so the two paths cannot
      // drift. An external caller uploads BEFORE it invokes, so there is no session yet —
      // hence the session-less form, which still carries the `/input/` segment that marks
      // the object as a source. The presigned PUT is scoped to exactly this key, so a
      // caller can only create this object.
      // Tagged with THIS caller (agent, issuer, sub): `GET /invoke/:agentId/download` serves an
      // `artifacts/uploads/<tag>/...` key to the caller whose tag it is, and to nobody else.
      const key = uploadKeyFor("", filename, now(), externalArtifactOwnerTag(agentId, issuer, verified.sub));
      const putUrl = await presignPut(key, contentType);
      const s3Uri = `s3://${uploadBucket}/${key}`;

      res.writeHead(200, {
        "content-type": "application/json",
        "access-control-allow-origin": "*",
      });
      res.end(
        JSON.stringify({
          putUrl,
          s3Uri,
          key,
          bucket: uploadBucket,
          contentType,
          expiresIn: UPLOAD_URL_TTL_SECONDS,
        }),
      );
    } catch (err: any) {
      console.error(
        `[agentcore-proxy] /invoke/:agentId/uploads error (agent=${agentId}):`,
        err?.message ?? err,
      );
      sendInvokeError(res, "internal_error", "Failed to create upload URL");
    }
  };
}
