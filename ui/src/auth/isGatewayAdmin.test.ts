// isGatewayAdmin.test.ts — Verify the pure-claims predicate that gates the
// gateway-admin UI surface.
//
// We focus on `isGatewayAdminFromClaims` here. The async/hook wrappers
// depend on a real `UserManager` and are exercised via the integration
// flow; the truth-table for the claim shape lives entirely in the pure
// helper, so testing it is enough to lock in the gating behavior.

import { describe, it, expect } from "vitest";
import {
  GATEWAY_ADMIN_GROUP,
  isGatewayAdminFromClaims,
} from "./isGatewayAdmin";

describe("isGatewayAdminFromClaims", () => {
  it("returns true when cognito:groups contains gateway-admins", () => {
    expect(
      isGatewayAdminFromClaims({ "cognito:groups": [GATEWAY_ADMIN_GROUP] }),
    ).toBe(true);
  });

  it("returns true when gateway-admins is one of several groups", () => {
    expect(
      isGatewayAdminFromClaims({
        "cognito:groups": ["readers", GATEWAY_ADMIN_GROUP, "writers"],
      }),
    ).toBe(true);
  });

  it("returns false when cognito:groups is missing", () => {
    expect(isGatewayAdminFromClaims({})).toBe(false);
  });

  it("returns false when cognito:groups is an empty array", () => {
    expect(isGatewayAdminFromClaims({ "cognito:groups": [] })).toBe(false);
  });

  it("returns false when cognito:groups exists but does not include gateway-admins", () => {
    expect(
      isGatewayAdminFromClaims({ "cognito:groups": ["readers", "writers"] }),
    ).toBe(false);
  });

  it("returns false when cognito:groups is a string instead of an array", () => {
    // Some Cognito configurations stringify groups; we require the array
    // form and treat anything else as not-admin.
    expect(
      isGatewayAdminFromClaims({ "cognito:groups": GATEWAY_ADMIN_GROUP }),
    ).toBe(false);
  });

  it("returns false for null or undefined claims", () => {
    expect(isGatewayAdminFromClaims(null)).toBe(false);
    expect(isGatewayAdminFromClaims(undefined)).toBe(false);
  });
});
