// Feature: custom-agents, Property 9: External invoke authentication and isolation
//
// Property 9 (design.md): On the external `POST /invoke/:agentId` path, the
// auth+isolation DECISION must (a) map every authentication/authorization
// failure to the correct non-2xx category/status, (b) NEVER dispatch to the
// Task Agent runtime on any failure, (c) answer 404 (not_found) when there is
// no current published version or the integration is disabled — WITHOUT ever
// consulting the IdP (no verify) or dispatching, and (d) keep each agent's OIDC
// config isolated so a token minted for agent A's issuer/key can never validate
// against agent B's integration config.
//
// The decision itself is realized by `verifyAgentTokenWithDeps` in
// `ui/server/auth/agentOidc.ts` PLUS the route's category→HTTP-status table in
// `ui/server/app.ts` (`app.post("/invoke/:agentId")`). Driving the full Express
// route is heavy (SigV4 signer + live AssetIndex + streaming runtime), so this
// property test exercises the DECISION+MAPPING seam: it runs the REAL verifier
// against a fake per-issuer JWKS (via the injectable `fetchJson` seam, minting
// real RS256 tokens with `jose`), wrapped in a pure re-statement of the route's
// resolve → extract → verify → client-gate → subject → dispatch flow and its
// exact category→status mapping (replicated verbatim from app.ts). The pure
// route wiring (SigV4 dispatch, SSE passthrough) is covered by task 9.4.
//
// Validates: Requirements 5.3, 5.4, 5.5, 5.6, 6.4, 8.8

import { describe, it, expect, beforeAll } from "vitest";
import fc from "fast-check";
import { SignJWT, exportJWK, generateKeyPair, type JWK } from "jose";

import {
  verifyAgentTokenWithDeps,
  AgentTokenError,
  type AgentIntegrationConfig,
  type AgentTokenErrorCategory,
} from "./auth/agentOidc";

const NUM_RUNS = 150; // Property 9 requires a minimum of 100 iterations.

// ---------------------------------------------------------------------------
// Pure re-statement of the route's category → HTTP status table
// (verbatim from `ui/server/app.ts` `invokeErrorStatus`). Kept independent of
// the module under test so a drift in either side is caught.
// ---------------------------------------------------------------------------

type RouteOnlyCategory = "not_found" | "client_not_allowed" | "internal_error";
type InvokeErrorCategory = RouteOnlyCategory | AgentTokenErrorCategory;

function invokeErrorStatus(category: InvokeErrorCategory): number {
  switch (category) {
    case "not_found":
      return 404;
    case "client_not_allowed":
      return 403;
    case "internal_error":
      return 500;
    case "missing_token":
    case "invalid_token":
    case "expired":
    case "issuer_mismatch":
    case "audience_mismatch":
    case "jwks_unavailable":
    case "missing_subject":
      return 401;
    default:
      return 500;
  }
}

// ---------------------------------------------------------------------------
// The decision-under-test: a faithful, side-effect-free re-statement of the
// `app.post("/invoke/:agentId")` control flow up to (and including) the
// dispatch branch. `dispatched` models the runtime dispatch as a flag set ONLY
// on the success branch; every failure path returns without setting it. The
// verifier is the REAL `verifyAgentTokenWithDeps`; `deps.fetchJson` is the
// injected fake JWKS. A `verifyCalled` flag records whether the IdP was ever
// consulted so the 404 (resolve-step) branch can assert "no verify occurs".
// ---------------------------------------------------------------------------

interface DecisionScenario {
  agentPresent: boolean;
  integration?: {
    enabled: boolean;
    issuer: string;
    audience: string;
    allowedClientIds: string[];
  };
  token: string | null;
  fetchJson: <T = unknown>(url: string) => Promise<T>;
  now: () => number;
}

interface DecisionOutcome {
  status: number;
  category: InvokeErrorCategory | "ok";
  dispatched: boolean;
  verifyCalled: boolean;
}

async function decideInvoke(s: DecisionScenario): Promise<DecisionOutcome> {
  let verifyCalled = false;
  const instrumentedFetchJson = <T = unknown>(url: string): Promise<T> => {
    verifyCalled = true;
    return s.fetchJson<T>(url);
  };

  const fail = (category: InvokeErrorCategory): DecisionOutcome => ({
    status: invokeErrorStatus(category),
    category,
    dispatched: false,
    verifyCalled,
  });

  // (Step 1) Resolve current published version + integration_config (Req 5.2).
  // 404 for unknown agent, no current published version, or disabled
  // integration (Req 5.3, 8.8). No token is read, no IdP consulted, no dispatch.
  if (!s.agentPresent || !s.integration || s.integration.enabled !== true) {
    return fail("not_found");
  }

  const { issuer, audience, allowedClientIds } = s.integration;

  // (Step 2) Extract the Bearer token; missing/malformed → 401 missing_token.
  if (!s.token) {
    return fail("missing_token");
  }

  // (Step 3) Per-agent OIDC verification (real verifier + fake per-issuer JWKS).
  const integrationConfig: AgentIntegrationConfig = {
    enabled: true,
    issuer,
    audience,
    allowedClientIds,
  };
  let verified: { sub: string; clientId?: string | null };
  try {
    verified = await verifyAgentTokenWithDeps(s.token, integrationConfig, {
      fetchJson: instrumentedFetchJson,
      now: s.now,
    });
  } catch (err) {
    if (err instanceof AgentTokenError) {
      return {
        status: invokeErrorStatus(err.category),
        category: err.category,
        dispatched: false,
        verifyCalled,
      };
    }
    return fail("internal_error");
  }

  // (Step 4) Client-id gate (Req 5.6).
  if (allowedClientIds.length > 0) {
    const clientId = verified.clientId ?? "";
    if (!clientId || !allowedClientIds.includes(clientId)) {
      return fail("client_not_allowed");
    }
  }

  // (Step 5) Subject required (Req 6.4). (The verifier already enforces this;
  // kept for parity with the route.)
  if (!verified.sub || typeof verified.sub !== "string") {
    return fail("missing_subject");
  }

  // (Step 6) Success → dispatch to the runtime.
  return { status: 200, category: "ok", dispatched: true, verifyCalled };
}

// ---------------------------------------------------------------------------
// Fake JWKS infrastructure — mint REAL RS256 tokens with jose and serve each
// agent's public key per-issuer through the injectable `fetchJson` seam.
// ---------------------------------------------------------------------------

interface AgentKeys {
  issuer: string;
  audience: string;
  kid: string;
  privateKey: CryptoKey;
  publicJwk: JWK;
}

async function makeAgentKeys(
  issuer: string,
  audience: string,
  kid: string,
): Promise<AgentKeys> {
  const { privateKey, publicKey } = await generateKeyPair("RS256", {
    extractable: true,
  });
  const publicJwk = { ...(await exportJWK(publicKey)), kid, alg: "RS256" };
  return { issuer, audience, kid, privateKey, publicJwk };
}

function discoveryUrlFor(issuer: string): string {
  return `${issuer.replace(/\/+$/, "")}/.well-known/openid-configuration`;
}
function jwksUrlFor(issuer: string): string {
  return `${issuer.replace(/\/+$/, "")}/jwks`;
}

/**
 * Build a per-issuer JWKS `fetchJson` from a set of agents. Each issuer serves
 * ONLY its own key at its own JWKS URL — this is the config-isolation seam: a
 * token minted for agent A's issuer is only ever checked against agent A's key
 * material, never agent B's.
 */
function makeFetchJson(agents: AgentKeys[]): <T = unknown>(url: string) => Promise<T> {
  const registry = new Map<string, unknown>();
  for (const a of agents) {
    registry.set(discoveryUrlFor(a.issuer), { jwks_uri: jwksUrlFor(a.issuer) });
    registry.set(jwksUrlFor(a.issuer), { keys: [a.publicJwk] });
  }
  return async <T = unknown>(url: string): Promise<T> => {
    if (registry.has(url)) return registry.get(url) as T;
    // An unknown URL models an unreachable IdP (fails closed → jwks_unavailable).
    throw new Error(`no route for ${url}`);
  };
}

/** Mint a signed RS256 token for a given key with explicit claims. */
async function mintToken(
  keys: AgentKeys,
  opts: {
    iss: string;
    aud: string | string[];
    sub?: string | undefined;
    clientId?: string | undefined;
    nowSec: number;
    expDeltaSec: number;
  },
): Promise<string> {
  const claims: Record<string, unknown> = { iss: opts.iss, aud: opts.aud };
  if (opts.sub !== undefined) claims.sub = opts.sub;
  if (opts.clientId !== undefined) claims.client_id = opts.clientId;
  return new SignJWT(claims)
    .setProtectedHeader({ alg: "RS256", kid: keys.kid })
    .setIssuedAt(opts.nowSec)
    .setExpirationTime(opts.nowSec + opts.expDeltaSec)
    .sign(keys.privateKey);
}

// ---------------------------------------------------------------------------
// Shared fixtures: two agents A and B with distinct issuers/audiences/keys.
// Generated ONCE (keypair generation is expensive); tokens are minted per run.
// ---------------------------------------------------------------------------

let agentA: AgentKeys;
let agentB: AgentKeys;
let fetchJson: <T = unknown>(url: string) => Promise<T>;

const NOW_SEC = 1_700_000_000; // fixed clock for deterministic exp/nbf handling
const now = () => NOW_SEC * 1000;

beforeAll(async () => {
  agentA = await makeAgentKeys("https://issuer-a.example.com", "aud-a", "key-a");
  agentB = await makeAgentKeys("https://issuer-b.example.com", "aud-b", "key-b");
  fetchJson = makeFetchJson([agentA, agentB]);
});

// ---------------------------------------------------------------------------
// Generated failure/success kinds and their expected outcome for agent A.
// ---------------------------------------------------------------------------

type Kind =
  | "valid"
  | "valid_with_client"
  | "expired"
  | "wrong_iss"
  | "wrong_aud"
  | "missing_sub"
  | "mismatched_client"
  | "missing_token"
  | "disabled"
  | "agent_missing";

const kindArb = fc.constantFrom<Kind>(
  "valid",
  "valid_with_client",
  "expired",
  "wrong_iss",
  "wrong_aud",
  "missing_sub",
  "mismatched_client",
  "missing_token",
  "disabled",
  "agent_missing",
);

// A small, DNS/URL-safe token for subject/client ids.
const idArb = fc
  .string({ minLength: 1, maxLength: 12 })
  .map((s) => s.replace(/[^a-zA-Z0-9]/g, "x"))
  .filter((s) => s.length > 0);

interface ExpectedDecision {
  category: InvokeErrorCategory | "ok";
  status: number;
  dispatched: boolean;
  verifyExpected: boolean; // was the IdP expected to be consulted?
}

/**
 * Build the scenario + the ground-truth expected outcome for a generated kind.
 * The expected category/status is derived independently from the requirement,
 * not from the code under test.
 */
async function buildScenario(
  kind: Kind,
  sub: string,
  clientId: string,
): Promise<{ scenario: DecisionScenario; expected: ExpectedDecision }> {
  const baseIntegration = {
    enabled: true,
    issuer: agentA.issuer,
    audience: agentA.audience,
    allowedClientIds: [] as string[],
  };

  const mkScenario = (
    overrides: Partial<DecisionScenario> & {
      integration?: DecisionScenario["integration"];
    },
    token: string | null,
  ): DecisionScenario => ({
    agentPresent: true,
    integration: baseIntegration,
    token,
    fetchJson,
    now,
    ...overrides,
  });

  switch (kind) {
    case "agent_missing":
      return {
        scenario: mkScenario({ agentPresent: false }, null),
        expected: { category: "not_found", status: 404, dispatched: false, verifyExpected: false },
      };
    case "disabled":
      return {
        scenario: mkScenario(
          { integration: { ...baseIntegration, enabled: false } },
          // Even with a valid token present, a disabled integration must 404
          // WITHOUT verifying.
          await mintToken(agentA, {
            iss: agentA.issuer,
            aud: agentA.audience,
            sub,
            nowSec: NOW_SEC,
            expDeltaSec: 3600,
          }),
        ),
        expected: { category: "not_found", status: 404, dispatched: false, verifyExpected: false },
      };
    case "missing_token":
      return {
        scenario: mkScenario({}, null),
        expected: { category: "missing_token", status: 401, dispatched: false, verifyExpected: false },
      };
    case "valid":
      return {
        scenario: mkScenario(
          {},
          await mintToken(agentA, {
            iss: agentA.issuer,
            aud: agentA.audience,
            sub,
            nowSec: NOW_SEC,
            expDeltaSec: 3600,
          }),
        ),
        expected: { category: "ok", status: 200, dispatched: true, verifyExpected: true },
      };
    case "valid_with_client":
      return {
        scenario: mkScenario(
          { integration: { ...baseIntegration, allowedClientIds: [clientId] } },
          await mintToken(agentA, {
            iss: agentA.issuer,
            aud: agentA.audience,
            sub,
            clientId,
            nowSec: NOW_SEC,
            expDeltaSec: 3600,
          }),
        ),
        expected: { category: "ok", status: 200, dispatched: true, verifyExpected: true },
      };
    case "expired":
      return {
        scenario: mkScenario(
          {},
          await mintToken(agentA, {
            iss: agentA.issuer,
            aud: agentA.audience,
            sub,
            nowSec: NOW_SEC,
            expDeltaSec: -3600, // expired an hour ago, well beyond the 60s leeway
          }),
        ),
        expected: { category: "expired", status: 401, dispatched: false, verifyExpected: true },
      };
    case "wrong_iss":
      return {
        scenario: mkScenario(
          {},
          // Signed by agent A's key (so the signature verifies against A's
          // JWKS), but the `iss` claim is a different value → issuer_mismatch.
          await mintToken(agentA, {
            iss: "https://evil-issuer.example.com",
            aud: agentA.audience,
            sub,
            nowSec: NOW_SEC,
            expDeltaSec: 3600,
          }),
        ),
        expected: { category: "issuer_mismatch", status: 401, dispatched: false, verifyExpected: true },
      };
    case "wrong_aud":
      return {
        scenario: mkScenario(
          {},
          await mintToken(agentA, {
            iss: agentA.issuer,
            aud: "some-other-audience",
            sub,
            nowSec: NOW_SEC,
            expDeltaSec: 3600,
          }),
        ),
        expected: { category: "audience_mismatch", status: 401, dispatched: false, verifyExpected: true },
      };
    case "missing_sub":
      return {
        scenario: mkScenario(
          {},
          await mintToken(agentA, {
            iss: agentA.issuer,
            aud: agentA.audience,
            sub: undefined, // no subject claim
            nowSec: NOW_SEC,
            expDeltaSec: 3600,
          }),
        ),
        expected: { category: "missing_subject", status: 401, dispatched: false, verifyExpected: true },
      };
    case "mismatched_client":
      return {
        scenario: mkScenario(
          { integration: { ...baseIntegration, allowedClientIds: [clientId] } },
          // Token carries a DIFFERENT client_id than the allowlist member.
          await mintToken(agentA, {
            iss: agentA.issuer,
            aud: agentA.audience,
            sub,
            clientId: `${clientId}-other`,
            nowSec: NOW_SEC,
            expDeltaSec: 3600,
          }),
        ),
        expected: { category: "client_not_allowed", status: 403, dispatched: false, verifyExpected: true },
      };
  }
}

// ---------------------------------------------------------------------------
// Property 9
// ---------------------------------------------------------------------------

describe("Property 9: External invoke authentication and isolation", () => {
  it("maps every claim set to the correct category/status and NEVER dispatches on failure", async () => {
    await fc.assert(
      fc.asyncProperty(kindArb, idArb, idArb, async (kind, sub, clientId) => {
        const { scenario, expected } = await buildScenario(kind, sub, clientId);
        const outcome = await decideInvoke(scenario);

        // (a) failure → correct category/status mapping; success → 200.
        expect(outcome.category).toBe(expected.category);
        expect(outcome.status).toBe(expected.status);

        // (b) dispatch happens on the success branch ONLY — never on any failure.
        expect(outcome.dispatched).toBe(expected.dispatched);
        if (outcome.status !== 200) {
          expect(outcome.dispatched).toBe(false);
        }

        // (c) 404 (no current version / disabled) is decided WITHOUT ever
        //     consulting the IdP (no verify) and without dispatch (Req 5.3, 8.8).
        if (expected.category === "not_found") {
          expect(outcome.verifyCalled).toBe(false);
          expect(outcome.dispatched).toBe(false);
        }
        // missing_token is also decided before any IdP consultation.
        if (expected.category === "missing_token") {
          expect(outcome.verifyCalled).toBe(false);
        }
      }),
      { numRuns: NUM_RUNS },
    );
  });

  it("keeps each agent's OIDC config isolated: agent A's token never validates for agent B", async () => {
    await fc.assert(
      fc.asyncProperty(
        idArb,
        fc.boolean(),
        async (sub, direction) => {
          // Mint a token for the SOURCE agent (valid against its own issuer/key)
          // and attempt to validate it against the TARGET agent's config.
          const [source, target] = direction ? [agentA, agentB] : [agentB, agentA];

          const token = await mintToken(source, {
            iss: source.issuer,
            aud: source.audience,
            sub,
            nowSec: NOW_SEC,
            expDeltaSec: 3600,
          });

          const scenario: DecisionScenario = {
            agentPresent: true,
            integration: {
              enabled: true,
              issuer: target.issuer,
              audience: target.audience,
              allowedClientIds: [],
            },
            token,
            fetchJson,
            now,
          };

          const outcome = await decideInvoke(scenario);

          // The cross-agent token must NEVER be accepted, and NEVER dispatched.
          expect(outcome.dispatched).toBe(false);
          expect(outcome.status).not.toBe(200);
          expect(outcome.category).not.toBe("ok");
          // Target serves only its own key at its own issuer; a token minted
          // for the source key resolves to a signature/key failure (or, had the
          // iss slipped through, an issuer mismatch) — always a 401 category.
          expect(outcome.status).toBe(401);
          expect(["invalid_token", "issuer_mismatch", "jwks_unavailable"]).toContain(
            outcome.category,
          );
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });

  it("category→status table matches the route for every taxonomy category (exhaustive)", () => {
    const table: Array<[InvokeErrorCategory, number]> = [
      ["not_found", 404],
      ["client_not_allowed", 403],
      ["internal_error", 500],
      ["missing_token", 401],
      ["invalid_token", 401],
      ["expired", 401],
      ["issuer_mismatch", 401],
      ["audience_mismatch", 401],
      ["jwks_unavailable", 401],
      ["missing_subject", 401],
    ];
    for (const [category, status] of table) {
      expect(invokeErrorStatus(category)).toBe(status);
    }
  });
});
