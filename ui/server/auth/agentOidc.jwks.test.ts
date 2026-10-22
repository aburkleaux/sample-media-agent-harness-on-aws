// Feature: custom-agents, Task 10.3 — example/integration tests for the
// per-agent OIDC verifier's JWKS-refresh, clock-skew, and fail-closed
// behaviors. These drive `verifyAgentTokenWithDeps` with injected `deps` seams
// (`fetchJson`, `now`, `cache`, `clockToleranceSec`) so no real network I/O or
// wall-clock time is involved. Real RS256 tokens are minted with `jose` against
// a generated keypair whose public JWK is served through the fake discovery +
// JWKS endpoints.
//
// Validates: Requirements 13.3, 13.4, 13.5

import { describe, it, expect, beforeAll } from "vitest";
import {
  SignJWT,
  exportJWK,
  generateKeyPair,
  type JSONWebKeySet,
  type JWK,
  type CryptoKey,
} from "jose";

import {
  verifyAgentTokenWithDeps,
  AgentTokenError,
  type AgentIntegrationConfig,
  type JwksCache,
} from "./agentOidc";
import { SsrfError } from "./ssrfFetch";

// ---------------------------------------------------------------------------
// Fixtures: issuer, audience, a fixed clock, and a generated RS256 keypair.
// ---------------------------------------------------------------------------

const ISSUER = "https://issuer.example.com";
const AUDIENCE = "https://api.example.com/agent-123";
const JWKS_URI = "https://issuer.example.com/jwks";
const DISCOVERY_URL = `${ISSUER}/.well-known/openid-configuration`;

// A fixed "now" so exp/nbf boundaries are deterministic.
const NOW_MS = 1_700_000_000_000;
const NOW_SEC = Math.floor(NOW_MS / 1000);
const CLOCK_TOLERANCE_SEC = 60;
const ALG = "RS256";
const KNOWN_KID = "key-known-1";

let privateKey: CryptoKey;
let publicJwk: JWK;

beforeAll(async () => {
  const pair = await generateKeyPair(ALG, { extractable: true });
  privateKey = pair.privateKey;
  const jwk = await exportJWK(pair.publicKey);
  jwk.kid = KNOWN_KID;
  jwk.alg = ALG;
  jwk.use = "sig";
  publicJwk = jwk;
});

/** The JWKS the fake IdP serves (contains only the KNOWN_KID public key). */
function servedJwks(): JSONWebKeySet {
  return { keys: [publicJwk] };
}

interface SignOptions {
  kid?: string;
  iss?: string;
  aud?: string;
  sub?: string;
  /** Absolute expiration in seconds since epoch. */
  expSec?: number;
  /** Absolute not-before in seconds since epoch. */
  nbfSec?: number;
  clientId?: string;
}

/** Mint a real RS256 JWT signed with the generated private key. */
async function signToken(opts: SignOptions = {}): Promise<string> {
  const builder = new SignJWT({
    ...(opts.clientId ? { client_id: opts.clientId } : {}),
  })
    .setProtectedHeader({ alg: ALG, kid: opts.kid ?? KNOWN_KID })
    .setIssuer(opts.iss ?? ISSUER)
    .setAudience(opts.aud ?? AUDIENCE)
    .setSubject(opts.sub ?? "user-abc")
    .setExpirationTime(opts.expSec ?? NOW_SEC + 3600);
  if (opts.nbfSec !== undefined) {
    builder.setNotBefore(opts.nbfSec);
  }
  return builder.sign(privateKey);
}

/**
 * Build a fake `fetchJson` that serves discovery + JWKS and counts how many
 * times each endpoint is hit. `jwksProvider` lets a test vary the served keys.
 */
function makeFetchJson(jwksProvider: () => JSONWebKeySet = servedJwks) {
  const counts = { discovery: 0, jwks: 0 };
  const fetchJson = async <T = unknown>(url: string): Promise<T> => {
    if (url === DISCOVERY_URL) {
      counts.discovery += 1;
      return { jwks_uri: JWKS_URI } as T;
    }
    if (url === JWKS_URI) {
      counts.jwks += 1;
      return jwksProvider() as T;
    }
    throw new Error(`unexpected fetch URL: ${url}`);
  };
  return { fetchJson, counts };
}

const config: AgentIntegrationConfig = {
  enabled: true,
  issuer: ISSUER,
  audience: AUDIENCE,
};

function baseDeps(fetchJson: <T = unknown>(url: string) => Promise<T>) {
  const cache: JwksCache = new Map();
  return {
    fetchJson,
    now: () => NOW_MS,
    clockToleranceSec: CLOCK_TOLERANCE_SEC,
    // Large TTL so nothing expires mid-test; refresh only happens on unknown kid.
    jwksTtlMs: 60 * 60 * 1000,
    cache,
  };
}

// ---------------------------------------------------------------------------
// Req 13.3 — unknown `kid` refreshes JWKS exactly once, then rejects.
// ---------------------------------------------------------------------------

describe("verifyAgentTokenWithDeps — unknown kid JWKS refresh (Req 13.3)", () => {
  it("refreshes the JWKS exactly once on an unknown kid, then rejects", async () => {
    const { fetchJson, counts } = makeFetchJson();
    // Token signed with a kid NOT present in the served JWKS.
    const token = await signToken({ kid: "kid-does-not-exist" });

    const promise = verifyAgentTokenWithDeps(token, config, baseDeps(fetchJson));

    await expect(promise).rejects.toBeInstanceOf(AgentTokenError);
    await expect(promise).rejects.toMatchObject({ category: "invalid_token" });

    // Initial fetch (cache miss) + exactly one refresh on the unknown kid = 2.
    expect(counts.jwks).toBe(2);
    expect(counts.discovery).toBe(2);
  });

  it("verifies a token whose kid is present in the served JWKS (sanity)", async () => {
    const { fetchJson, counts } = makeFetchJson();
    const token = await signToken({ kid: KNOWN_KID, sub: "user-known" });

    const result = await verifyAgentTokenWithDeps(
      token,
      config,
      baseDeps(fetchJson),
    );

    expect(result.sub).toBe("user-known");
    // Known kid resolves on the first fetch — no refresh needed.
    expect(counts.jwks).toBe(1);
    expect(counts.discovery).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// Req 13.4 — bounded clock-skew leeway on exp/nbf boundaries.
// ---------------------------------------------------------------------------

describe("verifyAgentTokenWithDeps — clock-skew leeway (Req 13.4)", () => {
  it("accepts a token expired 30s ago (within the 60s leeway)", async () => {
    const { fetchJson } = makeFetchJson();
    const token = await signToken({ expSec: NOW_SEC - 30, sub: "still-ok" });

    const result = await verifyAgentTokenWithDeps(
      token,
      config,
      baseDeps(fetchJson),
    );

    expect(result.sub).toBe("still-ok");
  });

  it("rejects a token expired 120s ago (beyond the 60s leeway) as expired", async () => {
    const { fetchJson } = makeFetchJson();
    const token = await signToken({ expSec: NOW_SEC - 120 });

    const promise = verifyAgentTokenWithDeps(token, config, baseDeps(fetchJson));

    await expect(promise).rejects.toBeInstanceOf(AgentTokenError);
    await expect(promise).rejects.toMatchObject({ category: "expired" });
  });

  it("accepts a token whose nbf is 30s in the future (within the leeway)", async () => {
    const { fetchJson } = makeFetchJson();
    const token = await signToken({
      nbfSec: NOW_SEC + 30,
      expSec: NOW_SEC + 3600,
      sub: "nbf-ok",
    });

    const result = await verifyAgentTokenWithDeps(
      token,
      config,
      baseDeps(fetchJson),
    );

    expect(result.sub).toBe("nbf-ok");
  });

  it("rejects a token whose nbf is 120s in the future (beyond the leeway)", async () => {
    const { fetchJson } = makeFetchJson();
    const token = await signToken({
      nbfSec: NOW_SEC + 120,
      expSec: NOW_SEC + 3600,
    });

    const promise = verifyAgentTokenWithDeps(token, config, baseDeps(fetchJson));

    await expect(promise).rejects.toBeInstanceOf(AgentTokenError);
    // A not-yet-valid (nbf) token maps to the generic invalid_token category.
    await expect(promise).rejects.toMatchObject({ category: "invalid_token" });
  });
});

// ---------------------------------------------------------------------------
// Req 13.5 — fail closed: any JWKS/discovery fetch failure → jwks_unavailable.
// ---------------------------------------------------------------------------

describe("verifyAgentTokenWithDeps — fail closed on IdP outage (Req 13.5)", () => {
  it("maps a thrown fetch (IdP outage) to jwks_unavailable", async () => {
    const fetchJson = async <T = unknown>(_url: string): Promise<T> => {
      throw new Error("connect ETIMEDOUT");
    };
    const token = await signToken();

    const promise = verifyAgentTokenWithDeps(token, config, baseDeps(fetchJson));

    await expect(promise).rejects.toBeInstanceOf(AgentTokenError);
    await expect(promise).rejects.toMatchObject({
      category: "jwks_unavailable",
    });
  });

  it("maps an SsrfError (blocked destination) to jwks_unavailable", async () => {
    const fetchJson = async <T = unknown>(url: string): Promise<T> => {
      throw new SsrfError("private", url);
    };
    const token = await signToken();

    const promise = verifyAgentTokenWithDeps(token, config, baseDeps(fetchJson));

    await expect(promise).rejects.toBeInstanceOf(AgentTokenError);
    await expect(promise).rejects.toMatchObject({
      category: "jwks_unavailable",
    });
  });

  it("maps a discovery response missing jwks_uri to jwks_unavailable", async () => {
    const fetchJson = async <T = unknown>(url: string): Promise<T> => {
      if (url === DISCOVERY_URL) return {} as T; // no jwks_uri
      throw new Error("should not reach JWKS fetch");
    };
    const token = await signToken();

    const promise = verifyAgentTokenWithDeps(token, config, baseDeps(fetchJson));

    await expect(promise).rejects.toBeInstanceOf(AgentTokenError);
    await expect(promise).rejects.toMatchObject({
      category: "jwks_unavailable",
    });
  });
});
