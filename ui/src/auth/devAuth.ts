// devAuth.ts — Local-only dev sign-in shim.
//
// When `VITE_DEV_BYPASS_AUTH === 'true'` the Lab UI replaces the Cognito
// hosted-UI redirect with a "Dev Login" button (see `AuthGate.tsx`). Clicking
// that button calls `signInDev()` here, which mints a fake HS256 JWT in the
// browser using the `jose` library (works via Web Crypto — `jsonwebtoken`
// would not). The token mirrors the claim shape Cognito would produce for an
// access token (`iss`, `aud`, `exp`, `iat`, `sub`, `token_use: 'access'`) so
// downstream code that inspects the JWT keeps working without special cases.
//
// The minted token is stashed in module-scope memory plus `sessionStorage` so
// a page refresh during local dev doesn't bounce you back to the Dev Login
// screen. Tokens never touch `localStorage` — same XSS-surface stance R2
// takes for the real Cognito flow.
//
// `getAccessToken()` in `authManager.ts` consults this shim first when dev
// bypass is on, so the API interceptor picks up the dev token transparently
// and we never have to instantiate the real Cognito `UserManager` (which
// would otherwise require `VITE_USER_POOL_ID` etc. to be configured).
//
// Safety: this entire module is gated on `VITE_DEV_BYPASS_AUTH === 'true'`.
// In production builds that flag is unset, `signInDev()` is unreachable from
// the UI, and the dev secret default is irrelevant. The matching server-side
// gate lives in `ui/agentcore-proxy.ts` (`DEV_BYPASS_AUTH=true`), which
// short-circuits JWT validation when set.
//
// Requirement: R7 — dev mode still works without provisioning a real user.

import { SignJWT } from "jose";

// Documented default — never use in a deployed environment. The proxy's
// `DEV_BYPASS_AUTH` short-circuits JWT validation, so this secret only has
// to exist; its value isn't checked anywhere downstream during local dev.
const DEFAULT_DEV_SECRET = "dev-secret-do-not-use-in-prod";

// Subject claim for the synthetic dev user. Matches the `LAB_DEFAULT_ACTOR_ID`
// fallback the proxy uses in dev bypass mode, so reads/writes line up.
const DEFAULT_DEV_SUB = "local-dev-user";

// 24h matches the spec brief — long enough that a dev session doesn't expire
// during typical use, short enough that it can't be mistaken for a real
// long-lived credential.
const DEV_TOKEN_TTL_SECONDS = 24 * 60 * 60;

// Mirror the access-token freshness threshold used by the real auth manager.
// If the cached dev token is within this window of expiry we treat it as
// unusable so the UI re-prompts for Dev Login rather than sending a
// just-about-to-expire token to the proxy.
const NEAR_EXPIRY_THRESHOLD_SECONDS = 60;

const TOKEN_STORAGE_KEY = "lab.devAuth.accessToken";
const EXPIRES_STORAGE_KEY = "lab.devAuth.expiresAt";

interface DevTokenState {
  token: string;
  expiresAt: number; // unix seconds
}

// Module-scoped cache. `sessionStorage` is the durable backing so a page
// refresh in local dev doesn't kick the user back to the Dev Login screen.
let inMemoryToken: DevTokenState | null = null;

export function isDevBypassEnabled(): boolean {
  return import.meta.env.VITE_DEV_BYPASS_AUTH === "true";
}

function loadFromSessionStorage(): DevTokenState | null {
  try {
    const token = window.sessionStorage.getItem(TOKEN_STORAGE_KEY);
    const expiresAtRaw = window.sessionStorage.getItem(EXPIRES_STORAGE_KEY);
    if (token === null || expiresAtRaw === null) {
      return null;
    }
    const expiresAt = Number(expiresAtRaw);
    if (!Number.isFinite(expiresAt)) {
      return null;
    }
    return { token, expiresAt };
  } catch {
    // sessionStorage unavailable (private browsing, SSR, etc.).
    return null;
  }
}

function saveToSessionStorage(state: DevTokenState | null): void {
  try {
    if (state === null) {
      window.sessionStorage.removeItem(TOKEN_STORAGE_KEY);
      window.sessionStorage.removeItem(EXPIRES_STORAGE_KEY);
    } else {
      window.sessionStorage.setItem(TOKEN_STORAGE_KEY, state.token);
      window.sessionStorage.setItem(EXPIRES_STORAGE_KEY, String(state.expiresAt));
    }
  } catch {
    // Same fallback as above — running purely from in-memory state is fine.
  }
}

function isStateUsable(state: DevTokenState | null): state is DevTokenState {
  if (state === null) return false;
  const nowSeconds = Math.floor(Date.now() / 1000);
  return state.expiresAt - nowSeconds > NEAR_EXPIRY_THRESHOLD_SECONDS;
}

function currentState(): DevTokenState | null {
  if (inMemoryToken !== null) {
    return inMemoryToken;
  }
  const persisted = loadFromSessionStorage();
  if (persisted !== null) {
    inMemoryToken = persisted;
  }
  return inMemoryToken;
}

/** True when there is a non-expired dev token cached. */
export function hasValidDevAuth(): boolean {
  return isStateUsable(currentState());
}

/**
 * Returns the cached dev access token if it's still valid. Returns `null`
 * when no token has been minted yet or the cached one has expired (or is
 * within the near-expiry threshold). Callers should redirect to the Dev
 * Login screen in that case — `AuthGate` handles this automatically.
 */
export function getDevAccessToken(): string | null {
  const state = currentState();
  if (!isStateUsable(state)) {
    return null;
  }
  return state.token;
}

/**
 * Mint a fake HS256 JWT with the standard Cognito-style claims and stash it
 * for `getDevAccessToken()` to return. The signature uses
 * `VITE_DEV_JWT_SECRET` if provided, falling back to a documented default
 * (`dev-secret-do-not-use-in-prod`).
 *
 * The `iss`/`aud` values are constructed to look like a real Cognito access
 * token (issuer = `https://cognito-idp.<region>.amazonaws.com/<userPoolId>`,
 * audience = the user pool client id). We don't require those env vars to be
 * set — placeholders are used for any that are missing — because the proxy's
 * `DEV_BYPASS_AUTH` short-circuit doesn't validate the token anyway.
 */
export async function signInDev(): Promise<void> {
  const region = import.meta.env.VITE_REGION ?? "us-west-2";
  const userPoolId = import.meta.env.VITE_USER_POOL_ID ?? "local-dev-pool";
  const audience =
    import.meta.env.VITE_USER_POOL_CLIENT_ID ?? "local-dev-client";
  const secret = import.meta.env.VITE_DEV_JWT_SECRET ?? DEFAULT_DEV_SECRET;
  const issuer = `https://cognito-idp.${region}.amazonaws.com/${userPoolId}`;

  const iat = Math.floor(Date.now() / 1000);
  const exp = iat + DEV_TOKEN_TTL_SECONDS;

  const token = await new SignJWT({
    token_use: "access",
    scope: "openid email profile",
    username: DEFAULT_DEV_SUB,
  })
    .setProtectedHeader({ alg: "HS256", typ: "JWT" })
    .setSubject(DEFAULT_DEV_SUB)
    .setIssuer(issuer)
    .setAudience(audience)
    .setIssuedAt(iat)
    .setExpirationTime(exp)
    .sign(new TextEncoder().encode(secret));

  const state: DevTokenState = { token, expiresAt: exp };
  inMemoryToken = state;
  saveToSessionStorage(state);
}

/** Drop the cached dev token. Useful for a "sign out" affordance in tests. */
export function signOutDev(): void {
  inMemoryToken = null;
  saveToSessionStorage(null);
}

// --- Test helpers ------------------------------------------------------------

/**
 * Reset both the in-memory and persisted dev auth state. Intended for unit
 * tests that need to start from a clean slate.
 */
export function __resetDevAuthForTests(): void {
  inMemoryToken = null;
  saveToSessionStorage(null);
}
