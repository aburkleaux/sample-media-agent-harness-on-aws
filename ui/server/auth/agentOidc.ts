/**
 * Per-agent, bring-your-own OIDC token verifier for the external invoke path
 * (`POST /invoke/:agentId`).
 *
 * This module is the seam the external invoke route (`ui/server/app.ts`) calls
 * to verify a Caller's bearer JWT against a Custom Agent's configured OIDC
 * issuer. The route wires the error taxonomy and SigV4/SSE dispatch around this
 * seam; this module owns the actual verification decision (design Component 4 +
 * Requirement 13):
 *
 *   - Per-issuer JWKS cache keyed by issuer URL, so agent A's keys can never
 *     validate a token minted for agent B's issuer (config isolation, 13 / 5.4).
 *   - Discovery: `{issuer}/.well-known/openid-configuration` → `jwks_uri`.
 *   - Algorithm pinning: only asymmetric RS256/ES256 are accepted; `alg=none`
 *     and symmetric HS* are rejected before any key material is consulted,
 *     defeating the key-confusion attack (13.1).
 *   - Signature, exact `iss`, `aud` (string OR array) containment, and `exp`
 *     verification, with both `client_id` and `azp` evaluated for the client id
 *     result (13.2, 5.4, 5.6).
 *   - Unknown-`kid`: the issuer's JWKS is refreshed exactly once before the
 *     token is rejected (13.3).
 *   - Bounded clock-skew leeway applied to `exp`/`nbf`/`iat` (13.4).
 *   - Fail closed: any discovery/JWKS/IdP outage maps to `jwks_unavailable`
 *     (HTTP 401), never a platform-outage signal (13.5).
 *
 * ⚠️ SSRF: EVERY discovery and JWKS fetch is routed through the SSRF-hardened
 * fetch wrapper (`./ssrfFetch`, Req 12). We deliberately do NOT use jose's
 * `createRemoteJWKSet` because it performs its own unguarded fetch; instead we
 * fetch the JWKS ourselves via `hardenedFetchJson`, build a LOCAL key set with
 * `jose.createLocalJWKSet`, and hand that to `jose.jwtVerify`. Any `SsrfError`
 * (or any other fetch failure) fails closed to `jwks_unavailable`.
 *
 * Runs in Node (the Express proxy), NOT in the browser. Never import from
 * `ui/src/**`.
 *
 * The pure verification decision (algorithm pinning, audience shape, and
 * client_id/azp resolution) is factored into exported helpers
 * ({@link isPinnedAsymmetricAlg}, {@link audienceContains},
 * {@link resolveClientId}, {@link evaluateClaimAcceptance}) so Property 15 can
 * drive it directly with generated claim sets, and the network/clock seams are
 * injectable via {@link verifyAgentTokenWithDeps} so the JWKS-refresh,
 * clock-skew, and fail-closed examples can run without real network I/O.
 */

import {
  jwtVerify,
  createLocalJWKSet,
  decodeProtectedHeader,
  errors as joseErrors,
  type JSONWebKeySet,
  type JWTPayload,
  type JWTVerifyGetKey,
} from "jose";

import { hardenedFetchJson, SsrfError } from "./ssrfFetch";

/**
 * The failure categories the verifier can raise, aligned with the external
 * invoke error taxonomy (design §Error Handling). Every one of these maps to
 * HTTP 401 at the route boundary. The 403 `client_not_allowed`, 404
 * `not_found`, and 500 `internal_error` categories are decided by the route
 * itself (not the verifier) and are therefore intentionally absent here.
 */
export type AgentTokenErrorCategory =
  | "missing_token"
  | "invalid_token"
  | "expired"
  | "issuer_mismatch"
  | "audience_mismatch"
  | "jwks_unavailable"
  | "missing_subject";

/**
 * The subset of a Custom Agent's `integration_config` the verifier needs.
 * Field names are camelCase to match the AssetIndex row shape
 * (`integrationConfig` map) the route reads.
 */
export interface AgentIntegrationConfig {
  enabled: boolean;
  issuer?: string | null;
  audience?: string | null;
  allowedClientIds?: string[];
}

/**
 * Successful verification result. `clientId` is the resolved `client_id`/`azp`
 * claim (used by the route's client-id gate), or null when neither is present.
 */
export interface VerifiedAgentToken {
  sub: string;
  clientId?: string | null;
}

/**
 * A typed verification failure whose {@link category} maps directly onto the
 * external invoke error taxonomy. The route inspects `instanceof
 * AgentTokenError` to distinguish an expected auth failure (→ 401) from an
 * unexpected proxy error (→ 500 `internal_error`).
 */
export class AgentTokenError extends Error {
  readonly category: AgentTokenErrorCategory;

  constructor(category: AgentTokenErrorCategory, message: string) {
    super(message);
    this.name = "AgentTokenError";
    this.category = category;
    // Restore prototype chain for `instanceof` under transpiled/bundled output.
    Object.setPrototypeOf(this, AgentTokenError.prototype);
  }
}

// ---------------------------------------------------------------------------
// Verifier tunables
// ---------------------------------------------------------------------------

/**
 * The pinned set of asymmetric signing algorithms the verifier accepts. Only
 * these are honored; `alg=none` and every symmetric (HS*) algorithm is rejected
 * so a token cannot be validated against public JWKS material via key confusion
 * (Req 13.1). Kept as a named constant so Property 15 can reference the exact
 * accept set.
 */
export const ALLOWED_ALGS: readonly string[] = ["RS256", "ES256"];

/** Default JWKS cache TTL — keys are re-fetched after this window elapses. */
const DEFAULT_JWKS_TTL_MS = 10 * 60 * 1000;

/**
 * Bounded clock-skew leeway (seconds) applied to `exp`/`nbf`/`iat` so a token
 * from an IdP with a slightly skewed clock is not spuriously rejected (13.4).
 */
const DEFAULT_CLOCK_TOLERANCE_SEC = 60;

// ---------------------------------------------------------------------------
// Pure decision helpers (exported for Property 15 / unit tests)
// ---------------------------------------------------------------------------

/**
 * True iff `alg` is a pinned asymmetric algorithm the verifier accepts. This is
 * the exact predicate behind Req 13.1: `alg=none` and any symmetric HS* (or any
 * other unpinned) algorithm returns false.
 */
export function isPinnedAsymmetricAlg(alg: string | undefined): boolean {
  return typeof alg === "string" && ALLOWED_ALGS.includes(alg);
}

/**
 * True iff the `aud` claim — accepted as either a string or an array of strings
 * — contains the configured audience (Req 13.2). When no audience is configured
 * the check is a no-op (returns true).
 */
export function audienceContains(
  aud: unknown,
  configuredAudience: string | null | undefined,
): boolean {
  if (!configuredAudience) return true; // nothing to enforce
  if (typeof aud === "string") return aud === configuredAudience;
  if (Array.isArray(aud)) {
    return aud.some((a) => typeof a === "string" && a === configuredAudience);
  }
  return false;
}

/**
 * Resolve the effective client id, evaluating BOTH `client_id` and `azp` claims
 * (Req 13.2). `client_id` takes precedence; `azp` is the fallback. Returns null
 * when neither is a non-empty string.
 */
export function resolveClientId(
  claims: { client_id?: unknown; azp?: unknown },
): string | null {
  const cid = claims.client_id;
  if (typeof cid === "string" && cid.length > 0) return cid;
  const azp = claims.azp;
  if (typeof azp === "string" && azp.length > 0) return azp;
  return null;
}

/** Input to the pure Property-15 acceptance decision. */
export interface ClaimAcceptanceInput {
  alg: string | undefined;
  aud?: unknown;
  client_id?: unknown;
  azp?: unknown;
  configuredAudience?: string | null;
  allowedClientIds?: string[];
}

/** Result of the pure Property-15 acceptance decision. */
export type ClaimAcceptanceResult =
  | { accepted: true; clientId: string | null }
  | {
      accepted: false;
      clientId: string | null;
      reason: AgentTokenErrorCategory | "client_not_allowed";
    };

/**
 * Pure accept/reject decision over an algorithm + claim set — the exact
 * predicate exercised by Property 15. A token is accepted **iff** its algorithm
 * is a pinned asymmetric algorithm AND (when an audience is configured) the
 * `aud` claim (string or array) contains it AND (when an allowlist is
 * configured) the resolved `client_id`/`azp` is a member. This function does
 * NOT perform signature/`iss`/`exp` verification (those need real crypto/time);
 * it isolates the algorithm/audience/client-shape logic so it is exhaustively
 * testable without network or key material.
 */
export function evaluateClaimAcceptance(
  input: ClaimAcceptanceInput,
): ClaimAcceptanceResult {
  const clientId = resolveClientId(input);

  if (!isPinnedAsymmetricAlg(input.alg)) {
    return { accepted: false, clientId, reason: "invalid_token" };
  }
  if (!audienceContains(input.aud, input.configuredAudience)) {
    return { accepted: false, clientId, reason: "audience_mismatch" };
  }
  if (input.allowedClientIds && input.allowedClientIds.length > 0) {
    if (!clientId || !input.allowedClientIds.includes(clientId)) {
      return { accepted: false, clientId, reason: "client_not_allowed" };
    }
  }
  return { accepted: true, clientId };
}

// ---------------------------------------------------------------------------
// Per-issuer JWKS cache + injectable network/clock seams
// ---------------------------------------------------------------------------

/** A cached, ready-to-use local key set for a single issuer. */
interface JwksCacheEntry {
  jwksUri: string;
  keySet: JWTVerifyGetKey;
  fetchedAt: number;
}

/** Per-issuer JWKS cache keyed by issuer URL string (config isolation). */
export type JwksCache = Map<string, JwksCacheEntry>;

/** Process-wide default cache used by the public {@link verifyAgentToken}. */
const defaultJwksCache: JwksCache = new Map();

/**
 * Injectable seams so the JWKS-refresh, clock-skew, and fail-closed behaviors
 * are testable without real network I/O or wall-clock time. Defaults route JSON
 * fetches through the SSRF-hardened wrapper and read the real clock.
 */
export interface AgentOidcDeps {
  /**
   * Fetch a URL and parse the response body as JSON. Defaults to
   * {@link hardenedFetchJson} (HTTPS-only, SSRF-classified, capped redirects).
   * Any thrown error is treated as an IdP outage → `jwks_unavailable`.
   */
  fetchJson?: <T = unknown>(url: string) => Promise<T>;
  /** Current time in ms since epoch. Defaults to `Date.now`. */
  now?: () => number;
  /** JWKS cache TTL in ms. Defaults to {@link DEFAULT_JWKS_TTL_MS}. */
  jwksTtlMs?: number;
  /** Clock-skew leeway in seconds. Defaults to {@link DEFAULT_CLOCK_TOLERANCE_SEC}. */
  clockToleranceSec?: number;
  /** JWKS cache to use. Defaults to the process-wide cache. */
  cache?: JwksCache;
}

async function defaultFetchJson<T>(url: string): Promise<T> {
  const { data } = await hardenedFetchJson<T>(url);
  return data;
}

/** Normalize an issuer URL for building the discovery URL (drop trailing `/`). */
function discoveryUrlFor(issuer: string): string {
  const trimmed = issuer.replace(/\/+$/, "");
  return `${trimmed}/.well-known/openid-configuration`;
}

/**
 * Fetch discovery + JWKS for an issuer and build a LOCAL jose key set. All
 * network access goes through the injected (SSRF-hardened) `fetchJson`; ANY
 * failure — SSRF rejection, discovery/JWKS non-2xx, malformed JSON, missing
 * `jwks_uri`, or empty key set — throws `jwks_unavailable` so the caller fails
 * closed (Req 13.5).
 */
async function fetchKeySet(
  issuer: string,
  fetchJson: <T = unknown>(url: string) => Promise<T>,
): Promise<{ keySet: JWTVerifyGetKey; jwksUri: string }> {
  let jwksUri: string;
  let jwks: JSONWebKeySet;
  try {
    const discovery = await fetchJson<{ jwks_uri?: unknown }>(
      discoveryUrlFor(issuer),
    );
    if (
      !discovery ||
      typeof discovery.jwks_uri !== "string" ||
      discovery.jwks_uri.length === 0
    ) {
      throw new AgentTokenError(
        "jwks_unavailable",
        `OIDC discovery for issuer '${issuer}' did not return a jwks_uri`,
      );
    }
    jwksUri = discovery.jwks_uri;
    jwks = await fetchJson<JSONWebKeySet>(jwksUri);
  } catch (err) {
    if (err instanceof AgentTokenError) throw err;
    // SsrfError, non-2xx, JSON parse failure, DNS/transport failure, etc. →
    // fail closed. The IdP is an external dependency; its outage is an auth
    // failure for this agent, not a platform outage (Req 13.5).
    const detail =
      err instanceof SsrfError
        ? `blocked/failed fetch (${err.reason})`
        : err instanceof Error
          ? err.message
          : String(err);
    throw new AgentTokenError(
      "jwks_unavailable",
      `Unable to fetch issuer '${issuer}' OIDC keys: ${detail}`,
    );
  }

  if (!jwks || !Array.isArray(jwks.keys) || jwks.keys.length === 0) {
    throw new AgentTokenError(
      "jwks_unavailable",
      `Issuer '${issuer}' JWKS contained no keys`,
    );
  }

  return { keySet: createLocalJWKSet(jwks), jwksUri };
}

/**
 * Resolve the local key set for an issuer, using the per-issuer cache. When
 * `forceRefresh` is set (or the cached entry is missing/expired), discovery +
 * JWKS are re-fetched and the cache is updated.
 */
async function getKeySet(
  issuer: string,
  deps: Required<Pick<AgentOidcDeps, "fetchJson" | "now" | "jwksTtlMs">> & {
    cache: JwksCache;
  },
  forceRefresh: boolean,
): Promise<JWTVerifyGetKey> {
  const { cache, now, jwksTtlMs, fetchJson } = deps;
  const cached = cache.get(issuer);
  if (
    !forceRefresh &&
    cached &&
    now() - cached.fetchedAt < jwksTtlMs
  ) {
    return cached.keySet;
  }
  const { keySet, jwksUri } = await fetchKeySet(issuer, fetchJson);
  cache.set(issuer, { keySet, jwksUri, fetchedAt: now() });
  return keySet;
}

// ---------------------------------------------------------------------------
// Verification
// ---------------------------------------------------------------------------

/**
 * Map a jose verification error onto the external invoke taxonomy. jose owns
 * signature, algorithm, and temporal (`exp`/`nbf`/`iat`) validation; we map its
 * typed errors to our categories. We check {@link joseErrors.JWTExpired} before
 * {@link joseErrors.JWTClaimValidationFailed} because the former extends the
 * latter.
 */
function mapJoseError(err: unknown): AgentTokenError {
  if (err instanceof joseErrors.JWTExpired) {
    return new AgentTokenError("expired", "Token is expired");
  }
  if (err instanceof joseErrors.JOSEAlgNotAllowed) {
    return new AgentTokenError(
      "invalid_token",
      "Token uses a non-pinned signing algorithm",
    );
  }
  if (err instanceof joseErrors.JWSSignatureVerificationFailed) {
    return new AgentTokenError("invalid_token", "Token signature is invalid");
  }
  if (err instanceof joseErrors.JWTClaimValidationFailed) {
    // nbf/iat (not-yet-valid / issued in the future) beyond the leeway.
    return new AgentTokenError(
      "invalid_token",
      `Token claim validation failed (${(err as { claim?: string }).claim ?? "claim"})`,
    );
  }
  if (
    err instanceof joseErrors.JWSInvalid ||
    err instanceof joseErrors.JWTInvalid
  ) {
    return new AgentTokenError("invalid_token", "Token is malformed");
  }
  return new AgentTokenError("invalid_token", "Token verification failed");
}

/**
 * Verify a Caller's bearer token against a Custom Agent's OIDC integration,
 * with injectable network/clock/cache seams for testing. The public
 * {@link verifyAgentToken} delegates here with production defaults (SSRF-hardened
 * fetch, real clock, process-wide cache).
 */
export async function verifyAgentTokenWithDeps(
  token: string,
  integrationConfig: AgentIntegrationConfig,
  deps: AgentOidcDeps = {},
): Promise<VerifiedAgentToken> {
  // (1) Token presence — a missing/blank/non-string token is `missing_token`.
  if (typeof token !== "string" || token.trim().length === 0) {
    throw new AgentTokenError("missing_token", "No bearer token provided");
  }

  // (2) Issuer must be configured. Without it we cannot reach an IdP, so fail
  //     closed as `jwks_unavailable` rather than silently accepting.
  const issuer = integrationConfig.issuer;
  if (typeof issuer !== "string" || issuer.trim().length === 0) {
    throw new AgentTokenError(
      "jwks_unavailable",
      "Agent integration has no configured issuer",
    );
  }

  // (3) Algorithm pinning BEFORE any key material is consulted: reject
  //     `alg=none` and symmetric HS* up front (key-confusion defense, 13.1).
  let header: { alg?: string; kid?: string };
  try {
    header = decodeProtectedHeader(token) as { alg?: string; kid?: string };
  } catch {
    throw new AgentTokenError("invalid_token", "Token header is malformed");
  }
  if (!isPinnedAsymmetricAlg(header.alg)) {
    throw new AgentTokenError(
      "invalid_token",
      `Token algorithm '${header.alg ?? "none"}' is not a pinned asymmetric algorithm`,
    );
  }

  const resolvedDeps = {
    fetchJson: deps.fetchJson ?? defaultFetchJson,
    now: deps.now ?? Date.now,
    jwksTtlMs: deps.jwksTtlMs ?? DEFAULT_JWKS_TTL_MS,
    clockToleranceSec: deps.clockToleranceSec ?? DEFAULT_CLOCK_TOLERANCE_SEC,
    cache: deps.cache ?? defaultJwksCache,
  };

  const verifyOptions = {
    algorithms: [...ALLOWED_ALGS],
    clockTolerance: resolvedDeps.clockToleranceSec,
    currentDate: new Date(resolvedDeps.now()),
  };

  // (4) Signature/temporal verification, with a single JWKS refresh on an
  //     unknown `kid` (13.3). First attempt uses the cached key set; if jose
  //     reports no matching key, refresh the issuer's JWKS exactly once and
  //     retry. A second no-matching-key is a hard `invalid_token`.
  let payload: JWTPayload;
  try {
    const keySet = await getKeySet(issuer, resolvedDeps, false);
    try {
      ({ payload } = await jwtVerify(token, keySet, verifyOptions));
    } catch (err) {
      if (err instanceof joseErrors.JWKSNoMatchingKey) {
        // Unknown kid → refresh JWKS once, then retry.
        const refreshed = await getKeySet(issuer, resolvedDeps, true);
        try {
          ({ payload } = await jwtVerify(token, refreshed, verifyOptions));
        } catch (retryErr) {
          if (retryErr instanceof joseErrors.JWKSNoMatchingKey) {
            throw new AgentTokenError(
              "invalid_token",
              "Token key id not found in issuer JWKS after refresh",
            );
          }
          throw mapJoseError(retryErr);
        }
      } else {
        throw mapJoseError(err);
      }
    }
  } catch (err) {
    // `getKeySet` throws AgentTokenError('jwks_unavailable') on IdP outage;
    // `jwtVerify`/`mapJoseError` throws AgentTokenError with a 401 category.
    if (err instanceof AgentTokenError) throw err;
    throw mapJoseError(err);
  }

  // (5) Exact issuer match (distinct category from a signature failure).
  if (payload.iss !== issuer) {
    throw new AgentTokenError(
      "issuer_mismatch",
      `Token issuer '${String(payload.iss)}' does not match configured issuer`,
    );
  }

  // (6) Audience containment — `aud` as string OR array (13.2).
  if (!audienceContains(payload.aud, integrationConfig.audience)) {
    throw new AgentTokenError(
      "audience_mismatch",
      "Token audience does not contain the configured audience",
    );
  }

  // (7) Subject required (Req 6.4). Resolve the client id from client_id/azp.
  const sub = payload.sub;
  if (typeof sub !== "string" || sub.length === 0) {
    throw new AgentTokenError(
      "missing_subject",
      "Validated token has no subject (sub) claim",
    );
  }

  const clientId = resolveClientId(payload as { client_id?: unknown; azp?: unknown });

  return { sub, clientId };
}

/**
 * Verify a Caller's bearer token against a Custom Agent's OIDC integration.
 *
 * @param token - the raw JWT extracted from the `Authorization: Bearer` header
 * @param integrationConfig - the resolved current-published-version integration config
 * @returns the verified `{ sub, clientId }` on success
 * @throws AgentTokenError - a typed failure mapping to the external invoke taxonomy
 */
export async function verifyAgentToken(
  token: string,
  integrationConfig: AgentIntegrationConfig,
): Promise<VerifiedAgentToken> {
  return verifyAgentTokenWithDeps(token, integrationConfig);
}
