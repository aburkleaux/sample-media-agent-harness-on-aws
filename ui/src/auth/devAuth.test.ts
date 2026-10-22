// devAuth.test.ts — Verify the local dev sign-in shim mints a JWT with the
// expected Cognito-shaped claims and caches it across calls.
//
// Runs in the default vitest node env. We don't pull in jsdom because the
// only browser API the module touches is `window.sessionStorage`, which is
// trivial to stub. The actual signature key isn't validated here — that's
// the proxy's job (and in dev bypass mode it's deliberately skipped). We
// only care that the shape of the token is right and that the cache works.

import { describe, it, expect, beforeEach, beforeAll } from "vitest";
import { decodeJwt, jwtVerify } from "jose";

// Minimal Storage shim — enough for devAuth's getItem/setItem/removeItem use.
class MemoryStorage {
  private map = new Map<string, string>();
  getItem(k: string): string | null {
    return this.map.has(k) ? (this.map.get(k) as string) : null;
  }
  setItem(k: string, v: string): void {
    this.map.set(k, String(v));
  }
  removeItem(k: string): void {
    this.map.delete(k);
  }
  clear(): void {
    this.map.clear();
  }
}

beforeAll(() => {
  // devAuth references `window.sessionStorage` directly, so wire up just
  // enough of the global to satisfy it under the node test environment.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).window = { sessionStorage: new MemoryStorage() };
});

// We intentionally import after the window stub is in place, so any
// module-load-time access to `window` resolves correctly.
const {
  __resetDevAuthForTests,
  getDevAccessToken,
  hasValidDevAuth,
  signInDev,
  signOutDev,
} = await import("./devAuth");

beforeEach(() => {
  __resetDevAuthForTests();
  // Ensure no stale env var from another test bleeds in.
  // Vitest exposes import.meta.env and lets us mutate it in tests.
  (import.meta.env as Record<string, string | undefined>).VITE_DEV_BYPASS_AUTH =
    "true";
  delete (import.meta.env as Record<string, string | undefined>).VITE_DEV_JWT_SECRET;
  delete (import.meta.env as Record<string, string | undefined>).VITE_USER_POOL_ID;
  delete (import.meta.env as Record<string, string | undefined>).VITE_USER_POOL_CLIENT_ID;
  delete (import.meta.env as Record<string, string | undefined>).VITE_REGION;
});

describe("signInDev", () => {
  it("mints a JWT with Cognito-shaped claims", async () => {
    await signInDev();

    const token = getDevAccessToken();
    expect(token).not.toBeNull();
    const claims = decodeJwt(token!);

    expect(claims.sub).toBe("local-dev-user");
    expect(claims.token_use).toBe("access");
    expect(claims.iss).toMatch(/^https:\/\/cognito-idp\.[^/]+\.amazonaws\.com\//);
    expect(claims.aud).toBeDefined();
    expect(typeof claims.iat).toBe("number");
    expect(typeof claims.exp).toBe("number");

    // ~24h expiry, give or take a few seconds for test wall-clock drift.
    const ttl = (claims.exp as number) - (claims.iat as number);
    expect(ttl).toBeGreaterThanOrEqual(24 * 60 * 60 - 5);
    expect(ttl).toBeLessThanOrEqual(24 * 60 * 60 + 5);
  });

  it("uses VITE_DEV_JWT_SECRET when provided so the signature verifies with it", async () => {
    (import.meta.env as Record<string, string | undefined>).VITE_DEV_JWT_SECRET =
      "custom-test-secret";
    (import.meta.env as Record<string, string | undefined>).VITE_USER_POOL_CLIENT_ID =
      "test-client-id";

    await signInDev();
    const token = getDevAccessToken();
    expect(token).not.toBeNull();

    // jwtVerify rejects the token if the signature doesn't validate against
    // the supplied key — proves we actually signed with the custom secret.
    const { payload } = await jwtVerify(
      token!,
      new TextEncoder().encode("custom-test-secret"),
      { audience: "test-client-id" },
    );
    expect(payload.sub).toBe("local-dev-user");
  });

  it("falls back to the documented default secret when VITE_DEV_JWT_SECRET is unset", async () => {
    await signInDev();
    const token = getDevAccessToken();
    expect(token).not.toBeNull();

    const { payload } = await jwtVerify(
      token!,
      new TextEncoder().encode("dev-secret-do-not-use-in-prod"),
    );
    expect(payload.sub).toBe("local-dev-user");
  });
});

describe("getDevAccessToken / hasValidDevAuth", () => {
  it("returns null when no dev sign-in has happened", () => {
    expect(getDevAccessToken()).toBeNull();
    expect(hasValidDevAuth()).toBe(false);
  });

  it("returns the cached token after sign-in", async () => {
    await signInDev();
    expect(hasValidDevAuth()).toBe(true);
    const first = getDevAccessToken();
    const second = getDevAccessToken();
    // Same minted token returned — no churn on every call.
    expect(first).toBe(second);
  });

  it("returns null after signOutDev", async () => {
    await signInDev();
    expect(hasValidDevAuth()).toBe(true);
    signOutDev();
    expect(getDevAccessToken()).toBeNull();
    expect(hasValidDevAuth()).toBe(false);
  });
});
