/**
 * Which objects an EXTERNAL caller of a Custom Agent may download through
 * `GET /invoke/:agentId/download`.
 *
 * The route used to check only that the object was in one of two buckets, so any verified caller
 * of any externally enabled agent could presign ANY key in the uploads/artifacts bucket or the
 * M2C output bucket for 12 hours: other users' session artifacts, `users/<actor>/…` lab assets,
 * plan manifests, other callers' media outputs.
 *
 * A caller's own work lives in two places, and each is scoped the way the rest of the app scopes
 * it:
 *
 *   * **The artifacts bucket**: only `artifacts/…` keys, under the SAME rule an internal user is
 *     held to (`authorizeArtifactKey`). A session-scoped key `artifacts/<session>/…` is readable only
 *     when `<session>` is THIS caller's derived session — `EXT_…`, a pure function of
 *     `(agentId, issuer, sub, conversation_id)`, so it can never alias another caller's. The
 *     caller's own session-less uploads (`artifacts/uploads/<tag>/…`, tagged by
 *     `externalArtifactOwnerTag(agentId, issuer, sub)` when `POST /invoke/:agentId/uploads` issued
 *     them) are readable by that caller only. `durable/` and `external/` stay readable, as they are
 *     for every signed-in internal user. Flat keys, legacy untagged uploads and other callers' or
 *     users' uploads are refused.
 *     A projects-session OUTPUT key (`projects/<pid>/sessions/<session>/output/…`, asset-lineage
 *     Phase 3 (a)) is readable on the same condition only — its session must be the caller's own.
 *     The project-member clause internal users get is NEVER applied to an external caller.
 *   * **The M2C output bucket**: only the outputs of a job THIS caller's session ran. An M2C job
 *     writes under `…/<execution-uuid>/…`, the last segment of its Step Functions execution ARN,
 *     and the job registry records which session each job belongs to.
 *
 * Pure — the job rows are passed in — so the rule is testable without DynamoDB.
 */
import { classifyArtifactKey, isArtifactKey, normalizeArtifactKey } from "../artifactUrlsRoute";
import { isProjectSessionOutputKey, parseSessionKey } from "../../src/shared/sessionLayout";

export interface ExternalScope {
  /** `deriveExternalSessionId(agentId, issuer, sub, conversationId)` for the verified caller. */
  sessionId: string;
  /** `externalArtifactOwnerTag(agentId, issuer, sub)`: the tag on this caller's own uploads. */
  ownerTag?: string;
  /** The agent's Owner — the resource actor every external job is registered under. */
  ownerActorId: string;
  uploadBucket: string;
  mediaBucket: string;
}

export type ScopeDecision =
  | { ok: true }
  | { ok: false; reason: "outside_artifacts" | "other_session" | "not_your_output" | "bucket_not_allowed" };

/** The last segment of an SFN execution ARN (the M2C output-prefix uuid), else null. */
export function executionIdOf(jobId: unknown): string | null {
  if (typeof jobId !== "string" || !jobId.startsWith("arn:aws:states:")) return null;
  const tail = jobId.split(":").pop() || "";
  return /^[A-Za-z0-9-]{8,}$/.test(tail) ? tail : null;
}

/** True when the M2C bucket key is an output of one of `jobs`. */
export function isOutputOfJobs(bucket: string, key: string, jobs: readonly Record<string, any>[]): boolean {
  const uri = `s3://${bucket}/${key}`;
  const segments = new Set(key.split("/"));
  for (const job of jobs) {
    const exec = executionIdOf(job.jobId);
    if (exec && segments.has(exec)) return true;
    const urls = Array.isArray(job.artifactUrls) ? job.artifactUrls : [];
    if (urls.some((u: unknown) => typeof u === "string" && u === uri)) return true;
  }
  return false;
}

/** Only this caller's jobs: the Owner's rows stamped with the caller's derived session. */
export function callerJobs(scope: ExternalScope, items: readonly Record<string, any>[]): Record<string, any>[] {
  return items.filter((it) => it?.actorId === scope.ownerActorId && it?.sessionId === scope.sessionId);
}

/**
 * Decide one ref. `loadJobs` is called only for an M2C-bucket ref (it costs a query), and returns
 * the Owner's recent job rows; this function narrows them to the caller.
 */
export async function authorizeExternalRef(
  ref: { bucket: string; key: string },
  scope: ExternalScope,
  loadJobs: () => Promise<readonly Record<string, any>[]>,
): Promise<ScopeDecision> {
  if (ref.bucket === scope.uploadBucket) {
    if (isProjectSessionOutputKey(ref.key)) {
      return parseSessionKey(ref.key)!.sid === scope.sessionId ? { ok: true } : { ok: false, reason: "other_session" };
    }
    if (!isArtifactKey(ref.key)) return { ok: false, reason: "outside_artifacts" };
    const owner = classifyArtifactKey(normalizeArtifactKey(ref.key));
    if (owner.kind === "shared") return { ok: true };
    if (owner.kind === "tag") {
      return scope.ownerTag && owner.tag === scope.ownerTag ? { ok: true } : { ok: false, reason: "other_session" };
    }
    if (owner.kind === "session" && owner.sessionId === scope.sessionId) return { ok: true };
    return { ok: false, reason: "other_session" };
  }
  if (scope.mediaBucket && ref.bucket === scope.mediaBucket) {
    const mine = callerJobs(scope, await loadJobs());
    return isOutputOfJobs(ref.bucket, ref.key, mine) ? { ok: true } : { ok: false, reason: "not_your_output" };
  }
  return { ok: false, reason: "bucket_not_allowed" };
}

/** The conversation id exactly as the invoke and jobs routes sanitize it (so the session matches). */
export function sanitizeConversationId(raw: unknown): string {
  return typeof raw === "string" ? raw.replace(/[^A-Za-z0-9._-]+/g, "").slice(0, 64) : "";
}
