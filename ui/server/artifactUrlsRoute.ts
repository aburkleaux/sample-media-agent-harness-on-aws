// POST /api/artifact-urls — authenticated resolution of artifact *refs* to short-lived presigned GET
// URLs, plus the single artifact-key authorization policy shared with `/api/artifact`.
//
// This is the backbone of the "no wall of broken links" media model (CloudFront content-security
// remediation, stable indirection):
//   * An artifact CloudFront URL (`https://<ARTIFACTS_DOMAIN>/artifacts/<key>`), an
//     `/api/artifact?key=artifacts/<key>` URL, and a bare `artifacts/<key>` are all STABLE
//     IDENTIFIERS. Nothing stores an expiring URL.
//   * The renderer resolves a batch of refs to presigned URLs at play time via this route, and
//     re-resolves on any media load/playback error. Because the stored thing is permanent and the
//     URL is minted per request, an expired URL can never produce a dead link.
//
// Authorization policy (`authorizeArtifactKey`, used here AND by `/api/artifact` when
// ARTIFACTS_REQUIRE_AUTH=true):
//   * The key must start with `artifacts/` and contain no `..` or backslash, else `not_resolvable`.
//   * `artifacts/<session>/…` is readable by the OWNER of that session's row; another owner is
//     `not_found` (uniform: never reveals whether the key or session exists).
//   * An OWNER-TAGGED key (artifactOwner.ts) — a session-less upload `artifacts/uploads/<tag>/…` or a
//     session-less render `artifacts/render-<tag>-…/…` — is readable by the actor whose tag it is.
//   * The shared namespaces `artifacts/durable/` (retired copies) and `artifacts/external/` (fal
//     outputs, session-less by construction) stay readable by any authenticated actor.
//   * Everything else is UNATTRIBUTED: a flat key (`artifacts/<file>`), a legacy untagged upload
//     (`artifacts/uploads/input/…`), and a session with no row or a row without `actorId` (a legacy
//     `render-<ts>` dir, a deleted session). Those used to be readable by every signed-in user; now
//     they are `allow` only when the location is REGISTERED as an asset of a project the caller can
//     read (`authorizeUnattributed`, the same check an M2C_REFERENCE gets), else `not_found`.
//   * An ownership-lookup ERROR fails closed (`not_found`).
//
// One reserved namespace is NOT session-owned and NOT shared: `artifacts/m2c/<bucket>/<key>` is an
// M2C_REFERENCE — the object M2C wrote, referenced where it wrote it and never copied
// (asset-lineage D4/R4). It is served by presigning `<key>` in the M2C proxy bucket, and only to
// an actor who can read a PROJECT that references that location (`authorizeM2cRef`, below). It is
// deliberately not in SHARED_ARTIFACT_NAMESPACES: that set means "any authenticated actor".
//
// One sub-tree OUTSIDE `artifacts/` is also an artifact key (asset-lineage Phase 3 (a)): a session
// OUTPUT under the project layout, `projects/<pid>/sessions/<sid>/output/<file>`
// (`isProjectSessionOutputKey`). It is `allow` when the caller OWNS session <sid> or is a member of
// project <pid> (`checkProjectMember`, the same `checkAccess` `assets/urls` applies: deliverables are
// visible to project members, D3); anything else — including an unknown session owner, which a
// legacy key would allow — is `not_found`. No other `projects/` key is ever an artifact key: project
// ASSET bytes (`projects/<pid>/<assetId>/…`) stay reachable only by asset id.

import { GetObjectCommand, S3Client } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import { readCappedBody } from "./agentTestRoute";
import {
  ACTOR_OWNER_TAG_RE,
  EXTERNAL_OWNER_TAG_RE,
  artifactOwnerTag,
  ownerTagOfRenderSession,
} from "./artifactOwner";
import { documentViewUrl, isViewedDocumentKey, mintViewTicket } from "./artifacts/viewTicket";
import { checkAccess as realCheckAccess, projectsEnabled } from "./projects/access";
import { isProjectSessionOutputKey, parseSessionKey } from "../src/shared/sessionLayout";
import { pidFromMirrorSk, srcGuardKey, userPk, MIRROR_SK_PREFIX } from "./projects/schema";
import { dynamoTransactStoreFromEnv, keyId } from "./projects/store";
import type { TransactStore } from "./projects/store";

/** Presigned-GET lifetime (seconds). The ref is permanent; the URL is re-minted on demand. */
export const ARTIFACT_URL_TTL_S = 3600;

/** Max keys per request. The client batches to this; a larger body is a 400. */
export const ARTIFACT_URL_MAX_KEYS = 100;

/** Longest accepted body: 100 keys of ~128 chars plus JSON overhead is well under this. */
const ARTIFACT_URLS_MAX_BODY_BYTES = 64 * 1024;

/** Session-less namespaces under `artifacts/` that any authenticated actor may read. */
export const SHARED_ARTIFACT_NAMESPACES: ReadonlySet<string> = new Set(["durable", "external"]);

/** The session-less upload namespace: `artifacts/uploads/<owner tag>/input/…` (legacy: `uploads/input/…`). */
export const UPLOADS_ARTIFACT_NAMESPACE = "uploads";

// ------------------------------------------------------------------ M2C references (D4/R4)

/** The reserved namespace of an M2C_REFERENCE: `artifacts/m2c/<bucket>/<key>`. */
export const M2C_ARTIFACT_PREFIX = "artifacts/m2c/";

/** S3 bucket naming: lower-case letters, digits, dots, hyphens, 3–63 characters. */
const S3_BUCKET_RE = /^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$/;

/** The bucket and key an M2C_REFERENCE names. Twin of `media.artifact_fetch.m2c_artifact_ref`. */
export interface M2cRef {
  bucket: string;
  key: string;
}

/** The reference key for `bucket`/`key`, or null when they cannot name one object. */
export function m2cArtifactKey(bucket: unknown, key: unknown): string | null {
  if (typeof bucket !== "string" || typeof key !== "string") return null;
  const b = bucket.trim().toLowerCase();
  const k = key.trim().replace(/^\/+/, "");
  if (!S3_BUCKET_RE.test(b)) return null;
  if (!k || k.endsWith("/") || k.includes("..") || k.includes("\\")) return null;
  return `${M2C_ARTIFACT_PREFIX}${b}/${k}`;
}

/** The `{bucket, key}` an `artifacts/m2c/<bucket>/<key>` reference names, else null. */
export function parseM2cArtifactKey(key: unknown): M2cRef | null {
  if (typeof key !== "string" || !key.startsWith(M2C_ARTIFACT_PREFIX)) return null;
  const rest = key.slice(M2C_ARTIFACT_PREFIX.length);
  const slash = rest.indexOf("/");
  if (slash <= 0) return null;
  const ref = { bucket: rest.slice(0, slash), key: rest.slice(slash + 1) };
  return m2cArtifactKey(ref.bucket, ref.key) === key ? ref : null;
}

/** True when `key` is an M2C_REFERENCE (shape only; authorization is `authorizeM2cRef`'s job). */
export function isM2cArtifactKey(key: unknown): boolean {
  return parseM2cArtifactKey(key) !== null;
}

/**
 * Decide whether `actorId` may read the object an M2C_REFERENCE names. Fail-closed: anything
 * other than a positive answer is `not_found`, the same code a missing object gets.
 */
export type AuthorizeM2cRef = (ref: M2cRef, actorId: string) => Promise<ArtifactAccess>;

/** What `defaultAuthorizeM2cRef` needs; injected so the policy is testable without AWS. */
export interface M2cAuthorizeDeps {
  /** The ONE bucket an M2C_REFERENCE may name; "" refuses every reference. */
  m2cBucket: string;
  /** Reader of the projects table (mirror rows + `SRC#` guards). */
  store: Pick<TransactStore, "query" | "batchGet"> | null;
  /** Project membership check; defaults to the real `checkAccess` (same one `assets/urls` uses). */
  checkAccess?: (actorSub: string, projectId: string) => Promise<{ ok: boolean }>;
}

/** Most projects one actor's membership is searched over (one `BatchGetItem` is 100 keys). */
export const M2C_PROJECT_SCAN_LIMIT = 100;

/**
 * The authorization gate for an M2C_REFERENCE (asset-lineage R1.2): the location must be
 * REGISTERED as an asset of a project the actor can read.
 *
 * The projects table's `SRC#sha256(bucket/key)` guard maps a bucket + key to its asset, but it is
 * keyed per project (`PROJECT#<pid> / SRC#…`), so there is no global reverse index. We therefore
 * look only where the answer could ever grant access: the actor's OWN project mirrors
 * (`USER#<sub> / PROJECT#<pid>`), one `BatchGetItem` of their guard rows, then the same
 * `checkAccess` the `POST /projects/:pid/assets/urls` route applies to the project that has one.
 * A reference no accessible project registers is refused — the uniform `not_found`.
 */
/**
 * A TTS voice's PREVIEW clip in the M2C proxy bucket: a seeded catalog sample or a registered clone's preview.
 * It belongs to the shared voice catalog, not to a project, so it is never registered as an asset and the
 * project check below always refused it -- an audition gallery of `s3://` previews played nothing (staging
 * 2be13758). Every actor can already list the catalog and fetch these through `get_voice`, so serving exactly
 * these keys to an authenticated actor exposes nothing new. Narrow on purpose: one file name, uuid-named dirs.
 */
const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
export const VOICE_PREVIEW_KEY_RE = new RegExp(
  `^(?:micro-workflow/tts-on-aws/voices/${UUID}|_model_artefacts/voice_catalog/[A-Za-z0-9_-]{1,64}/${UUID})/preview\\.wav$`, "i");

export function defaultAuthorizeM2cRef(deps: M2cAuthorizeDeps): AuthorizeM2cRef {
  const access = deps.checkAccess ?? realCheckAccess;
  return async (ref, actorId) => {
    if (!deps.m2cBucket || ref.bucket !== deps.m2cBucket || !actorId) return "not_found";
    if (VOICE_PREVIEW_KEY_RE.test(ref.key)) return "allow";
    if (!deps.store) return "not_found";
    try {
      const mirrors = await deps.store.query(userPk(actorId), MIRROR_SK_PREFIX);
      const pids = mirrors
        .map((m) => pidFromMirrorSk(String(m.SK)))
        .filter((p): p is string => !!p)
        .slice(0, M2C_PROJECT_SCAN_LIMIT);
      if (!pids.length) return "not_found";
      const guards = await deps.store.batchGet(pids.map((pid) => srcGuardKey(pid, ref.bucket, ref.key)));
      const registered = new Set(guards.filter((g) => g.assetId).map((g) => keyId(g)));
      for (const pid of pids) {
        if (!registered.has(keyId(srcGuardKey(pid, ref.bucket, ref.key)))) continue;
        if ((await access(actorId, pid)).ok) return "allow";
      }
      return "not_found";
    } catch (e) {
      console.error("[artifact-urls] m2c authorization failed:", (e as Error)?.message ?? e);
      return "not_found"; // fail closed
    }
  };
}

/**
 * Decide whether `actorId` may read an UNATTRIBUTED key in the artifacts bucket (see the file header).
 * Fail-closed like the M2C check: anything but a positive answer is `not_found`.
 */
export type AuthorizeUnattributed = (key: string, actorId: string) => Promise<ArtifactAccess>;

/** What `defaultAuthorizeUnattributed` needs; injected so the policy is testable without AWS. */
export interface UnattributedAuthorizeDeps {
  /** The artifacts bucket the key lives in; "" refuses everything. */
  bucket: string;
  store: Pick<TransactStore, "query" | "batchGet"> | null;
  checkAccess?: (actorSub: string, projectId: string) => Promise<{ ok: boolean }>;
}

/**
 * An unattributed key is readable when it is REGISTERED (`SRC#` guard) as an asset of a project the
 * actor can read: the same lookup `defaultAuthorizeM2cRef` makes for an M2C location, over the
 * artifacts bucket. That keeps legacy outputs a project filed (an old session's deliverables, a
 * model-named folder) reachable by its members, and nothing else.
 */
export function defaultAuthorizeUnattributed(deps: UnattributedAuthorizeDeps): AuthorizeUnattributed {
  const byLocation = defaultAuthorizeM2cRef({ m2cBucket: deps.bucket, store: deps.store, checkAccess: deps.checkAccess });
  return async (key, actorId) => {
    if (!deps.bucket || !actorId) return "not_found";
    // (The voice-preview exemption inside the M2C check matches only M2C-bucket keys, never `artifacts/…`.)
    return byLocation({ bucket: deps.bucket, key }, actorId);
  };
}

/** The default unattributed-key authorizer from env: the artifacts bucket + the projects table. */
export function envAuthorizeUnattributed(bucket: string, env: NodeJS.ProcessEnv = process.env): AuthorizeUnattributed {
  const store = projectsEnabled() ? dynamoTransactStoreFromEnv(env) : null;
  return defaultAuthorizeUnattributed({ bucket, store });
}

/** The default m2c authorizer from env: `MEDIA_PROXY_BUCKET` + the projects table. */
export function envAuthorizeM2cRef(env: NodeJS.ProcessEnv = process.env): AuthorizeM2cRef {
  const store = projectsEnabled() ? dynamoTransactStoreFromEnv(env) : null;
  return defaultAuthorizeM2cRef({ m2cBucket: (env.MEDIA_PROXY_BUCKET ?? "").trim(), store });
}

export interface PresignOptions {
  /** Presign with `ResponseContentDisposition: attachment; filename="<basename>"`. */
  download?: boolean;
}

/** Sign one GET with the proxy's own credentials. Injected so the handler is testable without AWS. */
export type PresignGet = (
  bucket: string,
  key: string,
  expiresIn: number,
  opts?: PresignOptions,
) => Promise<string>;

/**
 * Tri-state session ownership, from the session row:
 *   - `owned`        the row exists and its actorId is the caller;
 *   - `other-owner`  the row exists with a DIFFERENT actorId;
 *   - `unknown`      no row, or a row without actorId.
 * A lookup failure must THROW (the policy maps it to `not_found`), never return `unknown`.
 */
export type SessionOwnership = "owned" | "other-owner" | "unknown";
export type LookupSessionOwnership = (sessionId: string, actorId: string) => Promise<SessionOwnership>;

/** Outcome of the artifact-key policy for one key. */
export type ArtifactAccess = "allow" | "not_resolvable" | "not_found";

/**
 * True when `actorId` may read project `pid` (membership of an active project). MUST throw or return
 * false on any doubt; the policy maps an error to `not_found`.
 */
export type CheckProjectMember = (actorId: string, pid: string) => Promise<boolean>;

/** The real membership check (`checkAccess`); refuses everything where Projects are off. */
export function envCheckProjectMember(): CheckProjectMember {
  return async (actorId, pid) => projectsEnabled() && (await realCheckAccess(actorId, pid)).ok;
}

/**
 * True for every key an artifact route may serve: an `artifacts/` key (`isArtifactKey`) or a
 * projects-session OUTPUT key. Shape only. `isArtifactKey` itself keeps meaning `artifacts/`.
 */
export function isServableArtifactKey(key: unknown): key is string {
  return isArtifactKey(key) || isProjectSessionOutputKey(key);
}

/**
 * The agent runtime sometimes doubles the prefix (`artifacts/artifacts/…`); collapse it to the real
 * key. Same normalization `/api/artifact` has always applied.
 */
export function normalizeArtifactKey(key: string): string {
  return key.startsWith("artifacts/artifacts/") ? key.replace("artifacts/artifacts/", "artifacts/") : key;
}

/**
 * True when `key` is a well-formed artifact key: under `artifacts/`, non-empty remainder, no `..`
 * and no backslash. Shape only; ownership is `authorizeArtifactKey`'s job.
 */
export function isArtifactKey(key: unknown): key is string {
  if (typeof key !== "string" || !key) return false;
  if (key.includes("..") || key.includes("\\")) return false;
  return key.startsWith("artifacts/") && key.length > "artifacts/".length;
}

/**
 * The session a `artifacts/<sessionId>/…` key belongs to, or `null` when the key has no session
 * owner to check: malformed keys, flat keys (`artifacts/<file>`), and the shared namespaces.
 * Callers MUST validate with `isArtifactKey` first; a `null` here alone does not mean "refuse".
 */
/**
 * The session a `artifacts/<sessionId>/…` key belongs to, or `null` when the key has no session
 * owner to check: malformed keys, flat keys (`artifacts/<file>`), and the shared namespaces.
 * Callers MUST validate with `isArtifactKey` first; a `null` here alone does not mean "refuse".
 *
 * An M2C_REFERENCE yields its literal `m2c` namespace, which is no actor's session and therefore
 * matches none — so a caller comparing this against a known session refuses it, which is the
 * fail-closed answer. `authorizeArtifactKey` decides references before it gets here.
 */
export function sessionIdForKey(key: unknown): string | null {
  if (!isArtifactKey(key)) return null;
  const m = /^artifacts\/([^/]+)\/.+/.exec(key);
  if (!m) return null;
  const seg = m[1];
  if (SHARED_ARTIFACT_NAMESPACES.has(seg) || seg === UPLOADS_ARTIFACT_NAMESPACE) return null;
  return seg;
}

/**
 * Who an `artifacts/` key belongs to, from its shape alone (no lookup):
 *   - `shared`        `durable/` and `external/`;
 *   - `tag`           an owner-tagged key: `uploads/<tag>/…` or `render-<tag>-…/…` (artifactOwner.ts);
 *   - `session`       `artifacts/<session>/…` — the session row decides;
 *   - `unattributed`  a flat key, a legacy untagged upload, or any other `uploads/` shape.
 * Callers validate with `isArtifactKey` and normalize first. An M2C_REFERENCE classifies as the
 * `m2c` session, which no actor owns; the policies decide references before they get here.
 */
export type ArtifactKeyOwner =
  | { kind: "shared" }
  | { kind: "tag"; tag: string }
  | { kind: "session"; sessionId: string }
  | { kind: "unattributed" };

export function classifyArtifactKey(key: string): ArtifactKeyOwner {
  const m = /^artifacts\/([^/]+)\/(.+)$/.exec(key);
  if (!m) return { kind: "unattributed" }; // flat: artifacts/<file>
  const [, seg, rest] = m;
  if (SHARED_ARTIFACT_NAMESPACES.has(seg)) return { kind: "shared" };
  if (seg === UPLOADS_ARTIFACT_NAMESPACE) {
    const tag = rest.split("/")[0];
    return rest.includes("/") && (ACTOR_OWNER_TAG_RE.test(tag) || EXTERNAL_OWNER_TAG_RE.test(tag))
      ? { kind: "tag", tag }
      : { kind: "unattributed" };
  }
  const renderTag = ownerTagOfRenderSession(seg);
  if (renderTag) return { kind: "tag", tag: renderTag };
  return { kind: "session", sessionId: seg };
}

/**
 * Apply the artifact-key authorization policy (see the file header) for an AUTHENTICATED actor.
 * `cache` memoizes ownership per session within one request (an error is cached as `not_found`).
 * `authorizeM2c` decides M2C_REFERENCES; without it every reference is `not_found` (fail closed).
 */
export async function authorizeArtifactKey(
  key: unknown,
  actorId: string,
  lookup: LookupSessionOwnership,
  cache?: Map<string, ArtifactAccess>,
  authorizeM2c?: AuthorizeM2cRef,
  checkProjectMember?: CheckProjectMember,
  authorizeUnattributed?: AuthorizeUnattributed,
): Promise<ArtifactAccess> {
  if (isProjectSessionOutputKey(key)) {
    // Owner of the session OR member of the project; nothing else (an unknown owner is not enough).
    const sk = parseSessionKey(key)!;
    const id = `project-session:${sk.pid}/${sk.sid}`;
    const hit = cache?.get(id);
    if (hit) return hit;
    let access: ArtifactAccess = "not_found";
    try {
      if ((await lookup(sk.sid, actorId)) === "owned") access = "allow";
      else if (checkProjectMember && (await checkProjectMember(actorId, sk.pid!))) access = "allow";
    } catch {
      access = "not_found"; // fail closed
    }
    cache?.set(id, access);
    return access;
  }
  if (!isArtifactKey(key)) return "not_resolvable";
  const normalized = normalizeArtifactKey(key);
  const m2c = parseM2cArtifactKey(normalized);
  if (m2c) {
    const id = `m2c:${m2c.bucket}/${m2c.key}`;
    const hit = cache?.get(id);
    if (hit) return hit;
    const access = authorizeM2c ? await authorizeM2c(m2c, actorId) : "not_found";
    cache?.set(id, access);
    return access;
  }
  const owner = classifyArtifactKey(normalized);
  if (owner.kind === "shared") return "allow";
  if (owner.kind === "tag") return actorId && owner.tag === artifactOwnerTag(actorId) ? "allow" : "not_found";
  const unattributed = async (): Promise<ArtifactAccess> => {
    const id = `unattributed:${normalized}`;
    const hit = cache?.get(id);
    if (hit) return hit;
    let access: ArtifactAccess = "not_found";
    try {
      if (authorizeUnattributed) access = await authorizeUnattributed(normalized, actorId);
    } catch {
      access = "not_found"; // fail closed
    }
    cache?.set(id, access);
    return access;
  };
  if (owner.kind === "unattributed") return unattributed();
  const sessionId = owner.sessionId;
  // Per-session memo: "allow" (owned), "not_found" (another owner, or a lookup error), and
  // "not_resolvable" as the marker for an UNOWNED session, whose keys each go to `unattributed`.
  const cached = cache?.get(sessionId);
  if (cached) return cached === "not_resolvable" ? unattributed() : cached;
  let ownership: SessionOwnership;
  try {
    ownership = await lookup(sessionId, actorId);
  } catch {
    cache?.set(sessionId, "not_found");
    return "not_found"; // fail closed: an ownership-lookup error must never grant access
  }
  if (ownership === "owned") { cache?.set(sessionId, "allow"); return "allow"; }
  if (ownership === "other-owner") { cache?.set(sessionId, "not_found"); return "not_found"; }
  // No row, or a row without an owner: nothing says this is the caller's.
  cache?.set(sessionId, "not_resolvable");
  return unattributed();
}

export interface ResolveDeps {
  bucket: string;
  actorId: string;
  presignGet: PresignGet;
  lookupSessionOwnership: LookupSessionOwnership;
  download?: boolean;
  now?: () => number;
  /** The M2C proxy bucket an M2C_REFERENCE is presigned in; "" means none are resolvable. */
  m2cBucket?: string;
  /** Authorization for an M2C_REFERENCE; absent ⇒ every reference is refused. */
  authorizeM2c?: AuthorizeM2cRef;
  /** Project membership for a projects-session output key; absent ⇒ only the session owner. */
  checkProjectMember?: CheckProjectMember;
  /** Access to an UNATTRIBUTED key (see the file header); absent ⇒ every such key is refused. */
  authorizeUnattributed?: AuthorizeUnattributed;
  /**
   * When set, an HTML document resolves to a same-origin `/api/artifact?…&view=<ticket>` URL
   * instead of a presigned S3 URL. Only the proxy re-signs the links INSIDE a page, so a presigned
   * URL to the page serves its stored, expiring links; and the ticket (unlike `?access_token=`)
   * grants nothing beyond reading that one document. See artifacts/viewTicket.ts.
   */
  viewSecret?: string;
  /**
   * The caller will load the URL in a FRAME or a top-level navigation (the document viewer's
   * `<iframe>` and its "Open document" link), which cannot carry an Authorization header. Every key,
   * not only HTML/SVG, then resolves to a same-origin `/api/artifact?…&view=<ticket>` URL: the proxy
   * serves it with the isolation headers and a sniff-proof, policed content type
   * (artifacts/servePolicy.ts), and the ticket is the only credential in the URL. Requires
   * `viewSecret`; ignored for a download.
   */
  frame?: boolean;
}

/** One resolved ref. `expiresAt` is epoch SECONDS (what the client's re-sign machine compares against). */
export interface ResolvedUrl {
  url: string;
  expiresAt: number;
}

export interface ResolveResult {
  urls: Record<string, ResolvedUrl>;
  errors: Record<string, { code: "not_resolvable" | "not_found" }>;
}

/**
 * Resolve a batch of artifact refs to presigned URLs. Each key is independently authorized by
 * `authorizeArtifactKey`; a refused key gets a per-key `errors` entry (never also a `urls` entry).
 * Session ownership is looked up at most once per session per request. Results are keyed by the key
 * AS SENT; a doubled `artifacts/artifacts/` prefix is signed under its normalized form. An
 * M2C_REFERENCE is signed in the M2C proxy bucket under the key IT names — the bytes were never
 * copied into the artifacts bucket (asset-lineage D4).
 */
export async function resolveArtifactUrls(keys: string[], deps: ResolveDeps): Promise<ResolveResult> {
  const nowMs = (deps.now ?? Date.now)();
  const expiresAt = Math.floor(nowMs / 1000) + ARTIFACT_URL_TTL_S;
  const unique = [...new Set(keys)];
  const cache = new Map<string, ArtifactAccess>();
  const urls: Record<string, ResolvedUrl> = {};
  const errors: ResolveResult["errors"] = {};

  for (const key of unique) {
    const access = await authorizeArtifactKey(
      key, deps.actorId, deps.lookupSessionOwnership, cache, deps.authorizeM2c, deps.checkProjectMember,
      deps.authorizeUnattributed);
    if (access !== "allow") {
      errors[key] = { code: access };
      continue;
    }
    const normalized = normalizeArtifactKey(key);
    const m2c = parseM2cArtifactKey(normalized);
    if (m2c) {
      if (!deps.m2cBucket || m2c.bucket !== deps.m2cBucket) {
        errors[key] = { code: "not_found" };
        continue;
      }
      if (!deps.download && deps.viewSecret && deps.frame) {
        const t = mintViewTicket(normalized, deps.actorId, deps.viewSecret, nowMs);
        urls[key] = { url: documentViewUrl(normalized, t.ticket), expiresAt: t.expiresAt };
        continue;
      }
      const url = await deps.presignGet(
        m2c.bucket,
        m2c.key,
        ARTIFACT_URL_TTL_S,
        deps.download ? { download: true } : undefined,
      );
      urls[key] = { url, expiresAt };
      continue;
    }
    if (!deps.download && deps.viewSecret && (deps.frame || isViewedDocumentKey(normalized))) {
      const t = mintViewTicket(normalized, deps.actorId, deps.viewSecret, nowMs);
      urls[key] = { url: documentViewUrl(normalized, t.ticket), expiresAt: t.expiresAt };
      continue;
    }
    const url = await deps.presignGet(
      deps.bucket,
      normalized,
      ARTIFACT_URL_TTL_S,
      deps.download ? { download: true } : undefined,
    );
    urls[key] = { url, expiresAt };
  }

  return { urls, errors };
}

function sendJson(res: any, status: number, body: unknown): void {
  if (res.headersSent) return;
  res.writeHead(status, { "content-type": "application/json", "access-control-allow-origin": "*" });
  res.end(JSON.stringify(body));
}

/**
 * Read + validate the `{keys: string[], download?: boolean}` body (≤ ARTIFACT_URL_MAX_KEYS). Returns
 * the parsed request, or sends a 4xx and returns `null`.
 */
async function readRequest(req: any, res: any): Promise<{ keys: string[]; download: boolean; frame: boolean } | null> {
  let raw: Buffer;
  try {
    raw = await readCappedBody(req, ARTIFACT_URLS_MAX_BODY_BYTES);
  } catch (e: any) {
    const code = e?.code === "payload_too_large" ? 413 : 400;
    sendJson(res, code, { error: { code: "bad_request", message: "Could not read request body" } });
    return null;
  }
  const text = raw.toString("utf8").trim();
  let parsed: unknown;
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    sendJson(res, 400, { error: { code: "bad_request", message: "Body must be valid JSON" } });
    return null;
  }
  const body = (parsed ?? {}) as { keys?: unknown; download?: unknown; frame?: unknown };
  const keys = body.keys;
  if (!Array.isArray(keys) || keys.some((k) => typeof k !== "string")) {
    sendJson(res, 400, { error: { code: "bad_request", message: "keys must be an array of strings" } });
    return null;
  }
  if (keys.length > ARTIFACT_URL_MAX_KEYS) {
    sendJson(res, 400, {
      error: { code: "too_many_keys", message: `At most ${ARTIFACT_URL_MAX_KEYS} keys per request` },
    });
    return null;
  }
  return { keys: keys as string[], download: body.download === true, frame: body.frame === true };
}

export interface HandlerDeps {
  bucket: string;
  presignGet: PresignGet;
  lookupSessionOwnership: LookupSessionOwnership;
  now?: () => number;
  viewSecret?: string;
  /** The M2C proxy bucket (`MEDIA_PROXY_BUCKET`); "" refuses every M2C_REFERENCE. */
  m2cBucket?: string;
  /** Authorization for an M2C_REFERENCE; absent ⇒ every reference is refused. */
  authorizeM2c?: AuthorizeM2cRef;
  /** Project membership for a projects-session output key; absent ⇒ only the session owner. */
  checkProjectMember?: CheckProjectMember;
  /** Access to an UNATTRIBUTED key; absent ⇒ every such key is refused. */
  authorizeUnattributed?: AuthorizeUnattributed;
}

/**
 * `POST /api/artifact-urls` handler. Requires `req.actorId` (the `/api` auth middleware sets it; an
 * unauthenticated request is already 401'd before here). Returns `{urls, errors}`.
 */
export async function handleArtifactUrls(req: any, res: any, deps: HandlerDeps): Promise<void> {
  const actorId: string | undefined = req.actorId;
  if (!actorId) {
    sendJson(res, 401, { error: { code: "unauthorized", message: "No actor" } });
    return;
  }
  if (!deps.bucket) {
    sendJson(res, 200, { urls: {}, errors: {} }); // no artifacts bucket configured (local dev)
    return;
  }
  const request = await readRequest(req, res);
  if (request === null) return;
  try {
    const result = await resolveArtifactUrls(request.keys, {
      bucket: deps.bucket,
      actorId,
      presignGet: deps.presignGet,
      lookupSessionOwnership: deps.lookupSessionOwnership,
      download: request.download,
      frame: request.frame,
      now: deps.now,
      viewSecret: deps.viewSecret,
      m2cBucket: deps.m2cBucket,
      authorizeM2c: deps.authorizeM2c,
      checkProjectMember: deps.checkProjectMember,
      authorizeUnattributed: deps.authorizeUnattributed,
    });
    sendJson(res, 200, result);
  } catch (e) {
    console.error("[artifact-urls] error:", (e as Error)?.message ?? e);
    sendJson(res, 500, { error: { code: "internal_error", message: "Internal error" } });
  }
}

/**
 * `Content-Disposition` for a download presign: `attachment; filename="<basename>"`. Quotes,
 * backslashes, and control characters are replaced so the value can never break out of the quoted
 * string; a non-ASCII name additionally carries the RFC 5987 `filename*` form.
 */
export function attachmentDisposition(key: string): string {
  const base = key.split("/").pop() || "download";
  const ascii = base.replace(/["\\\x00-\x1f\x7f]/g, "_").replace(/[^\x20-\x7e]/g, "_");
  const disposition = `attachment; filename="${ascii}"`;
  return ascii === base ? disposition : `${disposition}; filename*=UTF-8''${encodeURIComponent(base)}`;
}

/** Build the default presigner from the proxy's own S3 credentials (same pattern as `/upload`). */
let cachedClient: S3Client | undefined;
export function defaultPresignGet(region: string): PresignGet {
  if (!cachedClient) cachedClient = new S3Client({ region: region || "us-west-2" });
  const client = cachedClient;
  return (bucket, key, expiresIn, opts) =>
    getSignedUrl(
      client,
      new GetObjectCommand({
        Bucket: bucket,
        Key: key,
        ...(opts?.download ? { ResponseContentDisposition: attachmentDisposition(key) } : {}),
      }),
      { expiresIn },
    );
}

/**
 * The artifact key an artifact URL identifies, or `null` when `ref` is not one of OUR artifact URLs:
 *   - `https://<artifactsDomain>/artifacts/<key>` (any query string is ignored), and
 *   - `/api/artifact?key=artifacts/<key>` (relative, or absolute on any host — the UI domain),
 *   - and the same two forms of a projects-session OUTPUT key
 *     (`projects/<pid>/sessions/<sid>/output/<file>`, asset-lineage Phase 3 (a)).
 * Bare keys are deliberately NOT matched: callers that rewrite URLs in place (manifests) must not
 * turn a stored key field into a URL. Returns a validated, normalized key.
 */
export function artifactKeyFromUrl(ref: unknown, artifactsDomain: string): string | null {
  if (typeof ref !== "string" || !ref) return null;
  let parsed: URL;
  try {
    parsed = new URL(ref, "http://relative.invalid");
  } catch {
    return null;
  }
  const isRelative = parsed.host === "relative.invalid";
  let key: string | null = null;
  if (parsed.pathname === "/api/artifact" || parsed.pathname === "/api/artifact/") {
    if (isRelative && !ref.startsWith("/")) return null;
    key = parsed.searchParams.get("key");
  } else if (
    !isRelative &&
    artifactsDomain &&
    parsed.protocol === "https:" &&
    parsed.host.toLowerCase() === artifactsDomain.toLowerCase() &&
    (parsed.pathname.startsWith("/artifacts/") || parsed.pathname.startsWith("/projects/"))
  ) {
    try {
      key = decodeURIComponent(parsed.pathname.slice(1));
    } catch {
      return null;
    }
  }
  if (isProjectSessionOutputKey(key)) return key;
  if (!isArtifactKey(key)) return null;
  return normalizeArtifactKey(key);
}
