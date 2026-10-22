// authManager.ts — Shared OIDC client for the Lab UI.
//
// Wraps `oidc-client-ts`'s UserManager configured against the Cognito user
// pool created in CDK. Other modules import the singleton `authManager` (or
// the helper functions) rather than constructing their own UserManager so
// that callback handlers and API interceptors all share the same in-memory
// user/token state.
//
// Cognito settings (region/userPoolId/userPoolClientId) are fetched at
// runtime from `GET /config` (served by `ui/server/app.ts`, sourced from the
// ECS task's own env vars) rather than baked into the Vite build as
// `VITE_*` vars. This means the same `ui/dist` build artifact works against
// any environment's User Pool — no per-environment CI build step needed.
//
// Token storage: oidc-client-ts defaults to sessionStorage for the
// UserManager state; the access/id/refresh tokens themselves live on the
// in-memory `User` object held by the manager. This matches R2's "tokens in
// memory, not localStorage" requirement.

import {
  User,
  UserManager,
  WebStorageStateStore,
  type UserManagerSettings,
} from "oidc-client-ts";
import { quotaSafeLocalStorage } from "../utils/safeStorage";
import { loadRuntimeConfig } from "../config/runtimeConfig";
import { getDevAccessToken, isDevBypassEnabled } from "./devAuth";

// Refresh the access token if it's within this many seconds of expiry.
const REFRESH_THRESHOLD_SECONDS = 60;

interface RuntimeAuthConfig {
  region: string;
  userPoolId: string;
  userPoolClientId: string;
  userPoolDomain: string;
}

async function fetchAuthConfig(): Promise<RuntimeAuthConfig> {
  // The shared page-wide /config load (config/runtimeConfig.ts): one request for sign-in, branding,
  // artifacts, and the Projects flag. It throws on a non-2xx, as this did.
  const data = (await loadRuntimeConfig()) as Partial<RuntimeAuthConfig>;
  const { region, userPoolId, userPoolClientId, userPoolDomain } = data ?? {};
  if (!region || !userPoolId || !userPoolClientId || !userPoolDomain) {
    throw new Error(
      "GET /config did not return region/userPoolId/userPoolClientId/userPoolDomain — is USER_POOL_ID/USER_POOL_ISSUER/USER_POOL_CLIENT_ID/USER_POOL_DOMAIN set on the backend?",
    );
  }
  return { region, userPoolId, userPoolClientId, userPoolDomain };
}

let _configPromise: Promise<RuntimeAuthConfig> | null = null;

function getAuthConfig(): Promise<RuntimeAuthConfig> {
  if (_configPromise === null) {
    _configPromise = fetchAuthConfig();
  }
  return _configPromise;
}

async function buildSettings(): Promise<UserManagerSettings> {
  const { region, userPoolId, userPoolClientId } = await getAuthConfig();
  const origin = window.location.origin;

  return {
    authority: `https://cognito-idp.${region}.amazonaws.com/${userPoolId}`,
    client_id: userPoolClientId,
    redirect_uri: `${origin}/auth/callback`,
    silent_redirect_uri: `${origin}/auth/silent-callback`,
    post_logout_redirect_uri: `${origin}/`,
    response_type: "code",
    scope: "openid email profile",
    // Persist the in-flight auth state across the Cognito redirect, but keep
    // tokens in the in-memory User object after sign-in completes.
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
    // The signin STATE (`oidc.<state>`) stays on localStorage so the silent-renew
    // iframe can read it, but via a quota-safe wrapper: a FULL localStorage used to
    // make this tiny write throw QuotaExceededError and brick sign-in entirely
    // (observed on staging). The wrapper prunes stale oidc.* + oldest session caches
    // and retries instead of propagating.
    stateStore: new WebStorageStateStore({ store: quotaSafeLocalStorage }),
  };
}

// Lazily construct the UserManager so importing this module in a non-browser
// test environment doesn't immediately fetch `/config` or fail on it.
let _userManagerPromise: Promise<UserManager> | null = null;

export function getUserManager(): Promise<UserManager> {
  if (_userManagerPromise === null) {
    _userManagerPromise = buildSettings().then(
      (settings) => new UserManager(settings),
    );
  }
  return _userManagerPromise;
}

/**
 * Test helper. Replace the underlying UserManager so unit tests can inject a
 * mock without exercising the real Cognito redirect flow (or the `/config`
 * fetch). Pass `null` to reset to the lazy default.
 */
export function __setUserManagerForTests(manager: UserManager | null): void {
  _userManagerPromise = manager === null ? null : Promise.resolve(manager);
}

// --- Public API --------------------------------------------------------------

export async function signinRedirect(returnTo?: string): Promise<void> {
  const manager = await getUserManager();
  // `returnTo` rides the OIDC `state` round-trip so the callback can send the user back to the
  // page they started on (e.g. a `/render-view?...` deep link), not always to `/`.
  return returnTo ? manager.signinRedirect({ state: { returnTo } }) : manager.signinRedirect();
}

/**
 * The same-origin path to land on after sign-in, from the `state` the callback returns. Only a
 * relative path is accepted (starts with one `/`, not `//`, no backslash, not an `/auth/` route),
 * so a crafted `state` can never redirect off-site or loop back into the callback.
 */
export function safeReturnTo(state: unknown): string {
  const raw = (state as { returnTo?: unknown } | null | undefined)?.returnTo;
  if (typeof raw !== "string") return "/";
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\")) return "/";
  if (raw.startsWith("/auth/")) return "/";
  return raw;
}

export async function signinCallback(url?: string): Promise<User | undefined> {
  return (await getUserManager()).signinCallback(url);
}

export async function signinSilent(): Promise<User | null> {
  return (await getUserManager()).signinSilent();
}

export async function signinSilentCallback(url?: string): Promise<void> {
  return (await getUserManager()).signinSilentCallback(url);
}

/**
 * Sign out via Cognito's hosted-UI `/logout` endpoint.
 *
 * Deliberately does NOT use `UserManager.signoutRedirect()`: that sends the
 * OIDC-standard `id_token_hint`/`post_logout_redirect_uri` params, but
 * Cognito's hosted-UI logout endpoint only understands its own proprietary
 * `client_id`/`logout_uri` pair — sending the standard params gets a 400
 * back from Cognito. `logout_uri` must exactly match one of the App Client's
 * registered Logout URLs (see `userPoolLogoutUrls` in the CDK stack).
 */
export async function signoutRedirect(): Promise<void> {
  const { userPoolDomain, userPoolClientId } = await getAuthConfig();
  await (await getUserManager()).removeUser();

  const url = new URL(`${userPoolDomain}/logout`);
  url.searchParams.set("client_id", userPoolClientId);
  url.searchParams.set("logout_uri", `${window.location.origin}/`);
  window.location.assign(url.toString());
}

export async function getUser(): Promise<User | null> {
  return (await getUserManager()).getUser();
}

/**
 * Return a usable access token. If the cached user has expired, or will
 * expire within REFRESH_THRESHOLD_SECONDS, trigger a silent renew first.
 *
 * Returns `null` when no user is signed in (caller should redirect to
 * sign-in) or when silent renew fails.
 */
export async function getAccessToken(): Promise<string | null> {
  // Local dev short-circuit: when `VITE_DEV_BYPASS_AUTH` is on, the API
  // interceptor must use the fake JWT minted by `signInDev()` rather than
  // the Cognito UserManager (which we may not have configured at all in
  // dev). Returns `null` when the dev token hasn't been minted yet so the
  // caller / AuthGate can show the Dev Login button.
  if (isDevBypassEnabled()) {
    return getDevAccessToken();
  }

  const manager = await getUserManager();
  const user = await manager.getUser();

  if (user === null || user.access_token === undefined) {
    return null;
  }

  if (isNearExpiry(user)) {
    try {
      const refreshed = await manager.signinSilent();
      return refreshed?.access_token ?? null;
    } catch {
      // Silent renew failed (refresh token expired, network error, etc.)
      // Caller is responsible for redirecting to sign-in on the next 401.
      return null;
    }
  }

  return user.access_token;
}

/**
 * Returns true when the user's access token is missing an expiry, already
 * expired, or will expire within REFRESH_THRESHOLD_SECONDS.
 */
export function isNearExpiry(user: User): boolean {
  // `expires_at` is a unix timestamp in seconds.
  const expiresAt = user.expires_at;
  if (expiresAt === undefined) {
    return true;
  }
  const nowSeconds = Math.floor(Date.now() / 1000);
  return expiresAt - nowSeconds <= REFRESH_THRESHOLD_SECONDS;
}

export const REFRESH_THRESHOLD_SECONDS_FOR_TESTS = REFRESH_THRESHOLD_SECONDS;
