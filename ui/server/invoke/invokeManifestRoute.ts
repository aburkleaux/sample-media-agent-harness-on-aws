/**
 * External render-manifest read route — `GET /invoke/:agentId/manifest?key=<s3key>`
 * (media-preview manifest fetch for the external OIDC caller realm).
 *
 * The external console (bring-your-own-OIDC) drives a published Custom Agent via
 * `POST /invoke/:agentId`. That stream targets the Task Agent, which emits raw
 * `{"type":"output_ref","s3_key":"artifacts/.../render/<id>.json"}` frames when a
 * render manifest is produced. To display the media, the console fetches the
 * manifest JSON — but CloudFront serves no CORS headers for the JSON object, so a
 * cross-origin browser fetch is blocked. This route serves that manifest JSON
 * same-origin, gated by the identical per-agent OIDC gate as `/invoke`.
 *
 * The media `track.src` / `sourceUrl` URLs INSIDE the manifest are stable artifact
 * identifiers (CloudFront `https://<ARTIFACTS_DOMAIN>/artifacts/…` or
 * `/api/artifact?key=artifacts/…`). When the artifacts CDN is private those are not
 * fetchable by the console, so after loading the manifest this route replaces each
 * one with a presigned GET (1h) of the upload bucket — per response, never stored.
 * `<video>/<audio>/<img>` load those directly (media loads are not CORS-gated).
 *
 * The `key` is constrained to render manifests under `artifacts/**​/render/*.json`
 * so a caller can never read arbitrary objects out of the bucket or traverse the
 * prefix — mirroring the sanitize discipline in `invokeUploadRoute.ts`.
 *
 * Registered OUTSIDE `authPaths` (alongside the other `/invoke/:agentId/*` routes)
 * so it does NOT hit the internal Cognito `authMiddleware`; auth is the per-agent
 * OIDC verify performed inside this handler.
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
import { artifactKeyFromUrl } from "../artifactUrlsRoute";
import { isProjectSessionOutputKey, parseSessionKey } from "../../src/shared/sessionLayout";

// ---- Injected dependency contracts ----

/**
 * Stream an S3 object's bytes to the response. Injected so the handler can be
 * integration-tested without a live S3. Resolves once the object has been fully
 * written (or throws so the handler can map NoSuchKey → 404 / other → 500).
 */
export type ReadObjectFn = (key: string, res: any) => Promise<void>;

export interface InvokeManifestHandlerDeps {
  /** Whether the AssetIndex client is configured (false → 500 internal_error). */
  assetIndexReady: boolean;
  /** Whether the S3 bucket the manifest lives in is configured (false → 500). */
  uploadReady: boolean;
  resolveCurrentPublishedAgent: ResolveCurrentPublishedAgentFn;
  verifyAgentToken: VerifyAgentTokenFn;
  /**
   * Legacy pass-through: stream the manifest bytes unchanged. Used only when the rewrite deps below
   * (`readObjectText` + `presignArtifact`) are not both provided.
   */
  readObject?: ReadObjectFn;
  /** Read a manifest object as UTF-8 text (throws NoSuchKey → 404). Enables the URL rewrite. */
  readObjectText?: ReadObjectTextFn;
  /** Presign a GET of `key` in the upload bucket for `expiresIn` seconds. Enables the URL rewrite. */
  presignArtifact?: PresignArtifactFn;
  /** The artifacts CloudFront domain, so `https://<domain>/artifacts/…` URLs are recognised. */
  artifactsDomain?: string;
}

/** Read an S3 object's full body as UTF-8 text. Injected for tests. */
export type ReadObjectTextFn = (key: string) => Promise<string>;

/** Presign one GET of an artifact key (upload bucket). Injected for tests. */
export type PresignArtifactFn = (key: string, expiresIn: number) => Promise<string>;

/** Lifetime of a presigned media URL handed to the external console (seconds). */
export const MANIFEST_MEDIA_URL_TTL_S = 3600;

/**
 * Return a copy of `manifest` in which EVERY string that is one of our artifact URLs
 * (`https://<artifactsDomain>/artifacts/…`, `/api/artifact?key=artifacts/…`) is replaced by a
 * presigned GET. Other strings — including bare `artifacts/…` keys, which manifests use as stored
 * identifiers — are left untouched. The stored manifest is never modified; this is a per-response
 * resolution, so the external console can play media while the artifacts CDN is private.
 *
 * A projects-session OUTPUT URL (asset-lineage Phase 3 (a)) is presigned ONLY when its session is
 * `ownSession` — the session the manifest itself belongs to. An external caller never gets the
 * project-member reach an internal user has.
 */
export async function presignManifestArtifactUrls(
  manifest: unknown,
  artifactsDomain: string,
  presign: PresignArtifactFn,
  ownSession?: string,
): Promise<unknown> {
  const signed = new Map<string, Promise<string>>();
  const walk = async (value: unknown): Promise<unknown> => {
    if (typeof value === "string") {
      const key = artifactKeyFromUrl(value, artifactsDomain);
      if (!key) return value;
      if (isProjectSessionOutputKey(key) && (!ownSession || parseSessionKey(key)?.sid !== ownSession)) {
        return value;
      }
      let url = signed.get(key);
      if (!url) {
        url = presign(key, MANIFEST_MEDIA_URL_TTL_S);
        signed.set(key, url);
      }
      return url;
    }
    if (Array.isArray(value)) return Promise.all(value.map(walk));
    if (value && typeof value === "object") {
      const out: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(value as Record<string, unknown>)) out[k] = await walk(v);
      return out;
    }
    return value;
  };
  return walk(manifest);
}

/**
 * A render manifest key: `artifacts/<session>/render/<id>.json`. The path segment
 * before `render/` is unconstrained (any non-empty run of non-slash chars can
 * repeat), but the final segment is a single allowlisted `.json` file and the
 * whole thing must sit under `artifacts/` — so `..`, absolute keys, and non-render
 * objects are all rejected.
 */
export const RENDER_MANIFEST_KEY_RE =
  /^artifacts\/(?:(?!\.\.?\/)[^/]+\/)+render\/[A-Za-z0-9._-]+\.json$/;

/**
 * Validate a caller-supplied manifest key. Returns the key when it is a
 * well-formed render-manifest key, else null. Rejects traversal (`..`), leading
 * slashes, doubled prefixes, and any object outside `artifacts/**​/render/*.json`.
 */
export function validateManifestKey(raw: unknown): string | null {
  if (typeof raw !== "string" || raw === "") return null;
  // Reject any traversal or backslash before the shape check (defence in depth;
  // the regex already forbids them, but be explicit about the intent).
  if (raw.includes("..") || raw.includes("\\")) return null;
  return RENDER_MANIFEST_KEY_RE.test(raw) ? raw : null;
}

/**
 * Build the `GET /invoke/:agentId/manifest` handler. The identity gate is
 * deliberately identical (and in the same order) to the jobs/upload routes:
 * resolve current published version + effective external-access → per-agent OIDC
 * verify → client-id/subject gates. On success it validates the `key` and streams
 * the manifest JSON same-origin.
 */
export function createInvokeManifestHandler(deps: InvokeManifestHandlerDeps) {
  const {
    assetIndexReady,
    uploadReady,
    resolveCurrentPublishedAgent,
    verifyAgentToken,
    readObject,
    readObjectText,
    presignArtifact,
    artifactsDomain = "",
  } = deps;

  return async function invokeManifestHandler(req: any, res: any): Promise<void> {
    const agentId = String(req.params?.agentId ?? "");

    try {
      // Misconfiguration → 500 (fails closed; no read).
      if (!assetIndexReady) {
        sendInvokeError(res, "internal_error", "Asset index is not configured");
        return;
      }
      if (!uploadReady) {
        sendInvokeError(res, "internal_error", "Artifact bucket is not configured");
        return;
      }

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
          `[agentcore-proxy] /invoke/:agentId/manifest verify error (agent=${agentId}):`,
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

      // Caller authorized. Validate the manifest key BEFORE any S3 access so a
      // caller can only ever read a render manifest under artifacts/**/render/.
      const urlObj = new URL(req.url || "/", "http://localhost");
      const key = validateManifestKey(urlObj.searchParams.get("key"));
      if (!key) {
        // 400 (not a leak — key shape is caller-controlled, not agent-existence).
        res.writeHead(400, {
          "content-type": "application/json",
          "access-control-allow-origin": "*",
        });
        res.end(
          JSON.stringify({
            error: {
              category: "invalid_key",
              message:
                "key must be a render manifest: artifacts/<...>/render/<id>.json",
            },
          }),
        );
        return;
      }

      // Rewrite mode: load the manifest, presign every artifact URL in it, answer the rewritten
      // JSON. A body that is not JSON is passed through unchanged (it carries no URLs we can find).
      if (readObjectText && presignArtifact) {
        const text = await readObjectText(key);
        let body = text;
        try {
          const rewritten = await presignManifestArtifactUrls(
            JSON.parse(text),
            artifactsDomain,
            presignArtifact,
            key.split("/")[1],
          );
          body = JSON.stringify(rewritten);
        } catch (err: any) {
          if (!(err instanceof SyntaxError)) throw err;
        }
        res.writeHead(200, {
          "content-type": "application/json",
          "access-control-allow-origin": "*",
          // The body now carries presigned URLs with a 1h lifetime: never cache it.
          "cache-control": "no-store",
        });
        res.end(body);
        return;
      }
      if (!readObject) {
        sendInvokeError(res, "internal_error", "Manifest reader is not configured");
        return;
      }
      // Stream the manifest JSON same-origin. readObject sets the 200 status +
      // headers and pipes the body; a NoSuchKey/other failure throws to the catch.
      await readObject(key, res);
    } catch (err: any) {
      const notFound =
        err?.name === "NoSuchKey" || err?.$metadata?.httpStatusCode === 404;
      if (notFound) {
        // Only reachable after the key passed validation — safe to report.
        if (!res.headersSent) {
          res.writeHead(404, {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
          });
          res.end(
            JSON.stringify({
              error: { category: "not_found", message: "Manifest not found" },
            }),
          );
        }
        return;
      }
      console.error(
        `[agentcore-proxy] /invoke/:agentId/manifest error (agent=${agentId}):`,
        err?.message ?? err,
      );
      if (!res.headersSent) {
        sendInvokeError(res, "internal_error", "Failed to read manifest");
      } else if (!res.writableEnded) {
        res.end();
      }
    }
  };
}
