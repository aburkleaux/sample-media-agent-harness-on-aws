// Feature: custom-agents, Property 15: JWT algorithm and audience acceptance
//
// Property 15 (design.md): For any token header and claim set, verification
// accepts the algorithm IF AND ONLY IF it is a pinned asymmetric algorithm
// (RS256/ES256) — `alg=none` and symmetric (HS*) algorithms are always
// rejected — AND the audience check succeeds IF AND ONLY IF the configured
// audience matches the `aud` claim treated as either a string or an array,
// evaluating both `client_id` and `azp` for client matching.
//
// This drives the pure decision helpers exported by `./agentOidc`
// (`isPinnedAsymmetricAlg`, `audienceContains`, `resolveClientId`, and the
// combined `evaluateClaimAcceptance`) directly, across generated algorithms,
// string|array audiences, and present/absent/matching/mismatched
// `client_id`/`azp` + `allowedClientIds`. Acceptance is checked against an
// INDEPENDENT oracle re-derived here from the Requirement 13.1/13.2 wording,
// sharing no code with the module under test.
//
// Validates: Requirements 13.1, 13.2

import { describe, it, expect } from "vitest";
import fc from "fast-check";

import {
  ALLOWED_ALGS,
  isPinnedAsymmetricAlg,
  audienceContains,
  resolveClientId,
  evaluateClaimAcceptance,
  type ClaimAcceptanceInput,
} from "./agentOidc";

const NUM_RUNS = 200; // Property 15 requires a minimum of 100 iterations.

// ---------------------------------------------------------------------------
// Independent oracle — re-derived from Req 13.1 (pinned asymmetric algs only)
// and 13.2 (`aud` string|array containment, client match over client_id/azp).
// Shares no code with the module under test.
// ---------------------------------------------------------------------------

const PINNED_ALGS = new Set(["RS256", "ES256"]);

/** Oracle: is `alg` a pinned asymmetric algorithm? (Req 13.1) */
function oracleIsPinnedAlg(alg: string | undefined): boolean {
  return typeof alg === "string" && PINNED_ALGS.has(alg);
}

/** Oracle: does `aud` (string OR array) contain the configured audience? (13.2) */
function oracleAudienceContains(
  aud: unknown,
  configuredAudience: string | null | undefined,
): boolean {
  if (!configuredAudience) return true; // nothing configured ⇒ no-op
  if (typeof aud === "string") return aud === configuredAudience;
  if (Array.isArray(aud)) {
    return aud.some((a) => a === configuredAudience);
  }
  return false;
}

/** Oracle: resolve client id with `client_id` precedence over `azp`. (13.2) */
function oracleResolveClientId(input: {
  client_id?: unknown;
  azp?: unknown;
}): string | null {
  const cid = input.client_id;
  if (typeof cid === "string" && cid.length > 0) return cid;
  const azp = input.azp;
  if (typeof azp === "string" && azp.length > 0) return azp;
  return null;
}

/** Oracle: full accept decision — the exact Property-15 predicate. */
function oracleAccepted(input: ClaimAcceptanceInput): boolean {
  if (!oracleIsPinnedAlg(input.alg)) return false;
  if (!oracleAudienceContains(input.aud, input.configuredAudience)) return false;
  if (input.allowedClientIds && input.allowedClientIds.length > 0) {
    const clientId = oracleResolveClientId(input);
    if (!clientId || !input.allowedClientIds.includes(clientId)) return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Generators — a small shared token pool so audiences and client ids collide
// often enough to exercise both the match and mismatch branches.
// ---------------------------------------------------------------------------

// alg: pinned (RS256/ES256), rejected asymmetric (RS512/ES384),
// symmetric HS*, alg=none, empty string, and undefined (missing header alg).
const algGen = fc.constantFrom<string | undefined>(
  "RS256",
  "ES256",
  "none",
  "HS256",
  "HS384",
  "RS512",
  "ES384",
  "",
  undefined,
);

// A small pool so `aud`, `configuredAudience`, `client_id`, `azp`, and
// `allowedClientIds` draw from overlapping values (matches happen frequently).
const token = fc.constantFrom("api://app", "aud-a", "aud-b", "client-1", "client-2", "other");

// aud: a single string, an array of strings (0..4, may include non-strings),
// or missing entirely.
const audGen = fc.oneof(
  token, // string form
  fc.array(fc.oneof(token, fc.constant(123 as unknown as string)), {
    maxLength: 4,
  }), // array form (occasionally with a non-string member)
  fc.constant(undefined), // missing
);

// configuredAudience: present (from the pool) or absent (null/undefined).
const configuredAudienceGen = fc.oneof(
  token,
  fc.constant(null),
  fc.constant(undefined),
);

// client_id / azp: present (from pool), empty string, or absent.
const clientClaimGen = fc.oneof(
  token,
  fc.constant(""),
  fc.constant(undefined),
);

// allowedClientIds: absent, empty, or a non-empty subset of the pool.
const allowedClientIdsGen = fc.oneof(
  fc.constant(undefined),
  fc.constant([] as string[]),
  fc.uniqueArray(token, { minLength: 1, maxLength: 4 }),
);

const inputGen: fc.Arbitrary<ClaimAcceptanceInput> = fc.record({
  alg: algGen,
  aud: audGen,
  client_id: clientClaimGen,
  azp: clientClaimGen,
  configuredAudience: configuredAudienceGen,
  allowedClientIds: allowedClientIdsGen,
});

// ---------------------------------------------------------------------------
// Property 15
// ---------------------------------------------------------------------------

describe("Property 15: JWT algorithm and audience acceptance", () => {
  it("evaluateClaimAcceptance accepts iff pinned alg AND audience match AND client allowed", () => {
    fc.assert(
      fc.property(inputGen, (input) => {
        const result = evaluateClaimAcceptance(input);
        expect(result.accepted).toBe(oracleAccepted(input));
      }),
      { numRuns: NUM_RUNS },
    );
  });

  it("rejection reason follows the alg → audience → client precedence", () => {
    // Guards the taxonomy the route depends on: an unpinned alg is reported
    // before an audience mismatch, which is reported before a client mismatch.
    fc.assert(
      fc.property(inputGen, (input) => {
        const result = evaluateClaimAcceptance(input);
        if (result.accepted) return; // covered by the acceptance property above

        const clientId = oracleResolveClientId(input);
        let expectedReason: string;
        if (!oracleIsPinnedAlg(input.alg)) {
          expectedReason = "invalid_token";
        } else if (!oracleAudienceContains(input.aud, input.configuredAudience)) {
          expectedReason = "audience_mismatch";
        } else {
          expectedReason = "client_not_allowed";
        }
        expect(result.reason).toBe(expectedReason);
        // The resolved client id is surfaced regardless of accept/reject.
        expect(result.clientId).toBe(clientId);
      }),
      { numRuns: NUM_RUNS },
    );
  });

  it("isPinnedAsymmetricAlg matches the independent oracle (RS256/ES256 only)", () => {
    // The exported accept set must be exactly {RS256, ES256}.
    expect([...ALLOWED_ALGS].sort()).toEqual(["ES256", "RS256"]);
    fc.assert(
      fc.property(algGen, (alg) => {
        expect(isPinnedAsymmetricAlg(alg)).toBe(oracleIsPinnedAlg(alg));
      }),
      { numRuns: NUM_RUNS },
    );
  });

  it("audienceContains matches the oracle for string and array `aud` shapes", () => {
    fc.assert(
      fc.property(audGen, configuredAudienceGen, (aud, configuredAudience) => {
        expect(audienceContains(aud, configuredAudience)).toBe(
          oracleAudienceContains(aud, configuredAudience),
        );
      }),
      { numRuns: NUM_RUNS },
    );
  });

  it("resolveClientId honors client_id precedence over azp", () => {
    fc.assert(
      fc.property(clientClaimGen, clientClaimGen, (client_id, azp) => {
        expect(resolveClientId({ client_id, azp })).toBe(
          oracleResolveClientId({ client_id, azp }),
        );
      }),
      { numRuns: NUM_RUNS },
    );
  });
});
