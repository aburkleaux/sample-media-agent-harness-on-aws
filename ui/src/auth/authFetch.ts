// authFetch.ts — Auth-aware `fetch` wrapper for the Lab UI.
//
// Drop-in replacement for `fetch` that:
//   1. Attaches `Authorization: Bearer <accessToken>` to every request whose
//      path matches one of the protected prefixes (`/api/*`, `/lab/*`,
//      `/sessions/*`, `/memory/*`, and — since the unauthenticated-route fix —
//      `/upload` and `/models`). Only genuinely public paths
//      (`/config`, `/api/artifact`) pass through unchanged.
//   2. On a 401 from a protected path, calls `signinSilent()` once to
//      refresh the access token and retries the same request with the new
//      token. If the silent refresh fails or the retry still comes back
//      401, the user is redirected to sign-in via `signinRedirect()`.
//   3. In `VITE_DEV_BYPASS_AUTH` mode, the silent-refresh/redirect dance is
//      skipped on 401 — the proxy's matching `DEV_BYPASS_AUTH` flag short-
//      circuits JWT validation, so a 401 there indicates a different
//      problem that an automatic redirect can't fix (and would crash the
//      dev session because Cognito isn't configured locally).
//
// Requirement: R2 — UI handles the OAuth2 authorization code flow and
// includes the access token on every targeted request.

import {
  getAccessToken,
  signinRedirect,
  signinSilent,
} from "./authManager";
import { isDevBypassEnabled } from "./devAuth";

/**
 * Path prefixes whose requests must carry the user's access token. These
 * mirror the four route families called out in the requirements doc (R2)
 * and in the proxy validator (task 6.2).
 *
 * Any of these as the *exact* request path also count — e.g. a `POST /lab`
 * matches `/lab/*` for the purposes of this interceptor.
 */
const PROTECTED_PREFIXES: readonly string[] = [
  "/api/",
  "/lab",
  "/sessions",
  // Direct timeline actions (`POST /media/actions`, ui/server/mediaActions.ts).
  "/media",
  // Project_Registry routes (list, create, detail, rename, delete, members, leave). The proxy
  // protects `/projects` in `authPaths` and takes the Actor from the verified token only, so
  // omitting it here would 401 every Projects call.
  "/projects",
  "/memory",
  "/catalog",
  // MCP server listing (Tools panel). Proxy-side control-plane read that
  // resolves the actor and fails closed — an unattributed request must 401
  // rather than be treated as anonymous.
  "/mcp",
  "/jobs",
  // NOTE: `/improve` is intentionally absent. The Improve surface moved to the
  // Capability API at `/control-plane/improve/*` (Step 3), which the `/control-plane`
  // prefix below already covers; the old `ui/server` proxy route is dead. This must
  // stay in lockstep with the proxy's `authPaths` (`authPaths.contract.test.ts`), so
  // `/improve` was dropped from both in the same change.
  // Render Agent invoke. The proxy protects /render in `authPaths`, so omitting
  // it here sends the request unauthenticated and every render 401s.
  "/render",
  // Owner-side Custom Agent test invoke. Must stay in step with the proxy's
  // `authPaths` — the route rejects an unattributed request, so omitting it
  // here would 401 every test run.
  "/agent-test",
  // Uploads write to S3 under the artifacts prefix and the result is served from a public
  // CloudFront domain, so an unauthenticated POST was anonymous content hosting + storage abuse.
  // The proxy now requires a JWT and derives the object key from the validated actor.
  // NOTE: `AgentService.upload` uses XMLHttpRequest (for upload-progress events), which does NOT
  // route through authFetch — it attaches the header itself. This entry covers any fetch-based caller.
  "/upload",
  // Bedrock inference-profile enumeration — no longer anonymous.
  "/models",
  // Admin (gateway-admins) surface: user directory + role management. The proxy
  // protects `/admin` in `authPaths` and then enforces the group with
  // `requireAdmin`; omitting it here would send admin calls unauthenticated and
  // 401 before the group check ever runs.
  "/admin",
  // Capability API (always-on Fargate service, `/control-plane/*`).
  //
  // Unlike every other entry here, this path does NOT go through `ui/server` —
  // an ALB listener rule forwards it straight to the Capability API's target
  // group, which is the point (one fewer application hop per read). So the
  // usual four registrations reduce to three: this prefix, the ALB rule, and
  // the CloudFront behavior, all in `infra/lib/agent-infra-stack.ts`. There is
  // no `app.ts` route or `authPaths` entry to add, because the proxy never sees
  // the request; the Capability API verifies the same Cognito access token
  // itself (`agent/src/capability_api/auth.py`).
  //
  // `/control-plane/config` is unauthenticated, but sending a token to it is
  // harmless and keeps this list a simple prefix match.
  "/control-plane",
];

/**
 * Returns the path portion of a fetch input, stripped of query/hash. Handles
 * absolute URLs (rare here — the UI talks to relative proxy paths), URL
 * objects, Request objects, and plain strings.
 */
function pathnameOf(input: RequestInfo | URL): string {
  let raw: string;
  if (typeof input === "string") {
    raw = input;
  } else if (input instanceof URL) {
    return input.pathname;
  } else {
    // Request
    raw = input.url;
  }

  // Absolute URL — let URL handle it.
  if (raw.includes("://")) {
    try {
      return new URL(raw).pathname;
    } catch {
      // fall through to manual strip
    }
  }

  const queryIdx = raw.search(/[?#]/);
  return queryIdx >= 0 ? raw.slice(0, queryIdx) : raw;
}

/**
 * True when `input` targets one of the four protected route families.
 * Exposed for tests; treat as internal.
 */
export function isProtectedPath(input: RequestInfo | URL): boolean {
  const path = pathnameOf(input);
  for (const prefix of PROTECTED_PREFIXES) {
    if (path === prefix) return true;
    if (path.startsWith(`${prefix}/`)) return true;
    // `/api/` already ends with `/`, so `path.startsWith("/api/")` is the
    // canonical check there. The other prefixes don't end with `/`, so the
    // `${prefix}/` check above covers them.
    if (prefix.endsWith("/") && path.startsWith(prefix)) return true;
  }
  return false;
}

/**
 * Build a new RequestInit (without mutating the caller's object) that has
 * the bearer header set. When `token` is `null`, any pre-existing
 * Authorization header is removed so we never leak a stale token from a
 * caller-supplied init.
 */
function withAuthHeader(
  init: RequestInit | undefined,
  token: string | null,
): RequestInit {
  const headers = new Headers(init?.headers ?? undefined);
  if (token !== null) {
    headers.set("Authorization", `Bearer ${token}`);
  } else {
    headers.delete("Authorization");
  }
  return { ...(init ?? {}), headers };
}

/**
 * Auth-aware `fetch`.
 *
 * Behavior matches `window.fetch` for paths that don't match the protected
 * prefixes. For protected paths, the access token is attached and 401s
 * trigger a single silent refresh + retry; persistent 401 (or a failed
 * silent refresh) redirects to sign-in.
 *
 * Note on retries: callers are expected to pass a string body in `init`
 * (which is reusable). The wrappers in `lab-api.ts` and `api.ts` all use
 * `JSON.stringify(...)` bodies, so the retry path is safe. We intentionally
 * do not handle `Request` objects with consumed streams — none of the call
 * sites in the Lab UI use that pattern.
 */
export async function authFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  if (!isProtectedPath(input)) {
    return fetch(input, init);
  }

  const token = await getAccessToken();
  const firstResponse = await fetch(input, withAuthHeader(init, token));

  if (firstResponse.status !== 401) {
    return firstResponse;
  }

  // 401 in dev bypass mode means the proxy isn't honoring the dev short-
  // circuit (or some upstream is rejecting the fake JWT). Don't try to talk
  // to Cognito — env vars aren't set and `signinRedirect` would crash. Hand
  // the 401 back to the caller so the dev can see the real failure.
  if (isDevBypassEnabled()) {
    return firstResponse;
  }

  // Try a silent refresh exactly once.
  let refreshedToken: string | null = null;
  try {
    const refreshedUser = await signinSilent();
    refreshedToken = refreshedUser?.access_token ?? null;
  } catch {
    refreshedToken = null;
  }

  if (refreshedToken === null) {
    // Silent renew failed — interactive redirect is the only recovery path.
    await signinRedirect();
    return firstResponse;
  }

  const retryResponse = await fetch(
    input,
    withAuthHeader(init, refreshedToken),
  );

  if (retryResponse.status === 401) {
    // The freshly-minted token still got rejected — redirect to sign-in.
    await signinRedirect();
  }

  return retryResponse;
}
