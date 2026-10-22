// vitest.setup.ts — global guards for the unit-test environment.
//
// Runs before every test file.
//
// Why this exists: CI failed with eight `TypeError: Failed to parse URL from
// /config` unhandled rejections while all 726 tests passed. Vitest exits
// non-zero on unhandled errors, so a green suite still failed the build.
//
// The chain is `useCurrentUser` / `useIsGatewayAdmin` → `getUserManager()` →
// `getAuthConfig()` → `fetch("/config")`. A relative URL has no base in Node, so
// the request throws. `useCurrentUser.ts` starts that chain with
// `void getUserManager().then(...)` — a floating promise with no owner, which is
// precisely the shape that surfaces as an unhandled rejection rather than a test
// failure.
//
// It is timing-dependent, which is why it was invisible locally: the suite runs
// in ~16s here against ~145s in CI, and locally `ui/.env` sets
// VITE_DEV_BYPASS_AUTH so the auth path short-circuits before ever fetching.
// Neither condition holds in CI. Chasing it per test file would be whack-a-mole:
// any file that renders a component touching auth can start the chain, and
// vitest attributes the rejection to whichever file happened to be running when
// it surfaced — not the one that caused it.
//
// So instead of mocking each caller, answer the request. A unit test should not
// reach the network at all; anything that tries gets a deterministic local
// response rather than a rejection that fails the build hours later.

import { beforeEach, afterAll, vi } from 'vitest';

/** Minimal shape `fetchAuthConfig` needs to resolve without a network. */
const AUTH_CONFIG = {
  userPoolId: 'test-pool',
  clientId: 'test-client',
  region: 'us-west-2',
  issuer: 'https://cognito-idp.us-west-2.amazonaws.com/test-pool',
  domain: 'test.auth.us-west-2.amazoncognito.com',
};

const realFetch = globalThis.fetch;

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

beforeEach(() => {
  // Reinstalled per test so a suite that stubs fetch itself still wins, and the
  // next file starts from a known state.
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : String((input as Request).url ?? input);

    // The specific request behind the CI failure.
    if (url === '/config' || url.endsWith('/config')) {
      return Promise.resolve(jsonResponse(AUTH_CONFIG));
    }

    // Any other relative URL would throw the same ERR_INVALID_URL. Reject with
    // a message that names the problem, rather than letting undici's opaque
    // "Failed to parse URL" surface as an unattributable unhandled rejection.
    if (url.startsWith('/')) {
      return Promise.reject(
        new Error(
          `Unit tests must not make network calls; got a relative request to "${url}". `
          + 'Mock the module that made it.',
        ),
      );
    }

    // Absolute URLs are left to whatever the test itself arranged.
    return realFetch(input, init);
  });
});

afterAll(() => {
  vi.unstubAllGlobals();
});
