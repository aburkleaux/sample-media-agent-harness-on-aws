/**
 * JWT validation for the Vite proxy.
 *
 * Runs in Node (the Vite dev server / proxy plugin), NOT in the browser.
 * Never import this from `ui/src/**` — `jsonwebtoken` and `jwks-rsa` rely on
 * Node crypto / fs and will not bundle for the browser.
 *
 * Behavior:
 *   - Initializes a single `jwks-rsa` client against
 *     `${USER_POOL_ISSUER}/.well-known/jwks.json` with caching + rate limiting.
 *   - `validateJwt(token)` verifies the token's signature, `iss`, and `exp`
 *     via `jsonwebtoken`, then manually checks `token_use === 'access'` and
 *     `client_id === USER_POOL_CLIENT_ID`. Resolves with the `sub` claim or
 *     throws.
 *
 * Cognito quirk:
 *   Cognito **access tokens** do not contain an `aud` claim — they expose
 *   the app client id under a non-standard `client_id` claim instead. Only
 *   Cognito ID tokens have `aud`. The Lab UI sends access tokens (per R2,
 *   so the token has the right scopes for downstream APIs), so we check
 *   `client_id`, not `aud`. See:
 *   https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-tokens-with-identity-providers.html#amazon-cognito-user-pools-using-the-access-token
 *
 * Env vars:
 *   USER_POOL_ISSUER     (required) — e.g. `https://cognito-idp.us-west-2.amazonaws.com/<pool-id>`
 *   USER_POOL_CLIENT_ID  (required) — the user pool app client id; used as
 *                                     the expected `client_id` claim
 */

import jwt, { type JwtHeader, type SigningKeyCallback } from "jsonwebtoken";
import jwksClient, { type JwksClient } from "jwks-rsa";

let cachedClient: JwksClient | null = null;
let cachedIssuer: string | null = null;

function getConfig(): { issuer: string; clientId: string } {
  const issuer = process.env.USER_POOL_ISSUER;
  const clientId = process.env.USER_POOL_CLIENT_ID;
  if (!issuer) {
    throw new Error(
      "[validateJwt] USER_POOL_ISSUER env var is required (e.g. https://cognito-idp.<region>.amazonaws.com/<pool-id>)"
    );
  }
  if (!clientId) {
    throw new Error(
      "[validateJwt] USER_POOL_CLIENT_ID env var is required (the user pool app client id)"
    );
  }
  return { issuer, clientId };
}

function getJwksClient(issuer: string): JwksClient {
  // Rebuild the client if the issuer changes (e.g. across tests).
  if (cachedClient && cachedIssuer === issuer) {
    return cachedClient;
  }
  cachedClient = jwksClient({
    jwksUri: `${issuer}/.well-known/jwks.json`,
    cache: true,
    cacheMaxEntries: 5,
    cacheMaxAge: 10 * 60 * 1000, // 10 minutes
    rateLimit: true,
    jwksRequestsPerMinute: 10,
  });
  cachedIssuer = issuer;
  return cachedClient;
}

/**
 * Verifies a Cognito-issued access token and returns its `sub` claim
 * plus any membership-bearing claims the runtime needs for authorization.
 *
 * Throws if the token is malformed, has an unknown `kid`, fails signature
 * verification, is expired, has the wrong `iss`, isn't an access token, or
 * was issued for a different app client.
 *
 * Returns:
 *   - `sub`     — Cognito user-pool subject (always present on access tokens)
 *   - `groups`  — `cognito:groups` claim, normalized to a string[]. Cognito
 *                 emits this as an array on access tokens **only when** the
 *                 user belongs to one or more groups; missing claim resolves
 *                 to an empty array. The runtime reads this for the
 *                 gateway-admin gate (see `_is_admin` in agent/src/main.py).
 */
export async function validateJwt(token: string): Promise<{ sub: string; groups: string[] }> {
  if (!token || typeof token !== "string") {
    throw new Error("[validateJwt] token must be a non-empty string");
  }

  const { issuer, clientId } = getConfig();
  const client = getJwksClient(issuer);

  const getKey = (header: JwtHeader, callback: SigningKeyCallback): void => {
    if (!header.kid) {
      callback(new Error("[validateJwt] token header missing 'kid'"));
      return;
    }
    client.getSigningKey(header.kid, (err, key) => {
      if (err || !key) {
        callback(err ?? new Error("[validateJwt] no signing key returned"));
        return;
      }
      callback(null, key.getPublicKey());
    });
  };

  return new Promise<{ sub: string; groups: string[] }>((resolve, reject) => {
    // We deliberately omit the `audience` option — Cognito access tokens
    // do not have an `aud` claim. The app client is asserted manually
    // against `client_id` after decoding.
    jwt.verify(
      token,
      getKey,
      {
        issuer,
        algorithms: ["RS256"],
      },
      (err, decoded) => {
        if (err) {
          reject(err);
          return;
        }
        if (!decoded || typeof decoded === "string") {
          reject(new Error("[validateJwt] decoded token is not an object"));
          return;
        }
        const claims = decoded as {
          sub?: unknown;
          token_use?: unknown;
          client_id?: unknown;
          "cognito:groups"?: unknown;
        };

        if (claims.token_use !== "access") {
          reject(
            new Error(
              `[validateJwt] expected access token (token_use='access'); got token_use=${String(claims.token_use)}`
            )
          );
          return;
        }
        if (claims.client_id !== clientId) {
          reject(
            new Error(
              "[validateJwt] token client_id does not match configured user pool client"
            )
          );
          return;
        }
        const sub = claims.sub;
        if (typeof sub !== "string" || sub.length === 0) {
          reject(new Error("[validateJwt] token missing 'sub' claim"));
          return;
        }
        // `cognito:groups` is emitted on access tokens only when the user
        // is in at least one group. Normalize to string[] so callers can
        // treat "no groups" and "missing claim" identically.
        const rawGroups = claims["cognito:groups"];
        const groups = Array.isArray(rawGroups)
          ? rawGroups.filter((g): g is string => typeof g === "string")
          : [];
        resolve({ sub, groups });
      }
    );
  });
}

/** Test-only hook to clear the cached JWKS client. */
export function _resetJwksClientForTests(): void {
  cachedClient = null;
  cachedIssuer = null;
}
