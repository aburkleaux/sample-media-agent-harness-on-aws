/**
 * Synth-time guard for the Search plane (media-harness-projects-access, design Component 6; task 8.9).
 *
 * `assertSearchPlane` throws, naming the missing element, unless the Search_Gateway is governed by a
 * Cedar engine in ENFORCE with exactly one policy whose statement equals the builder output, and a
 * CUSTOM_JWT authorizer carrying the issuer's discovery URL and the Search audience. It runs on the
 * in-memory values the ProjectsStack already holds (not a re-synthesized template), so it is a cheap,
 * non-recursive check that fails `cdk synth` rather than `cdk deploy` (Req 6.11).
 */

export interface SearchPlaneFacts {
  /** Whether the Cedar `CfnPolicyEngine` was created. */
  readonly hasPolicyEngine: boolean;
  /** The gateway's `PolicyEngineConfiguration.Mode` L1 override (expected `"ENFORCE"`). */
  readonly policyEngineMode: string | undefined;
  /** Every Cedar policy statement attached to the Search engine (expected exactly one). */
  readonly policyStatements: readonly string[];
  /** The statement `buildSearchPolicy(...)` produced for this env. */
  readonly expectedStatement: string;
  /** The policy's `validationMode` (expected `"FAIL_ON_ANY_FINDINGS"`). */
  readonly policyValidationMode: string | undefined;
  /** The CUSTOM_JWT authorizer's discovery URL (expected the OIDC issuer's discovery URL). */
  readonly authorizerDiscoveryUrl: string | undefined;
  /** The expected OIDC discovery URL. */
  readonly expectedDiscoveryUrl: string;
  /** The authorizer's `allowedAudience` list (expected to contain the Search audience). */
  readonly authorizerAllowedAudience: readonly string[] | undefined;
  /** The expected Search audience. */
  readonly expectedAudience: string;
  /** Whether the authorizer declares `allowedClients` (expected false — none). */
  readonly authorizerHasAllowedClients: boolean;
  /** Whether the gateway's MCP protocol config declares `searchType` (expected false — disabled). */
  readonly mcpHasSearchType: boolean;
}

export function assertSearchPlane(facts: SearchPlaneFacts): void {
  const fail = (what: string): never => {
    throw new Error(`assertSearchPlane: ${what}`);
  };

  if (!facts.hasPolicyEngine) fail("the Search Cedar policy engine is missing");

  if (facts.policyEngineMode !== "ENFORCE") {
    fail(`the gateway PolicyEngineConfiguration.Mode is ${JSON.stringify(facts.policyEngineMode)}, expected "ENFORCE"`);
  }

  if (facts.policyStatements.length !== 1) {
    fail(`expected exactly one Cedar policy on the Search engine, found ${facts.policyStatements.length}`);
  }

  if (facts.policyStatements[0] !== facts.expectedStatement) {
    fail("the Search policy statement does not equal the buildSearchPolicy output");
  }

  if (facts.policyValidationMode !== "FAIL_ON_ANY_FINDINGS") {
    fail(`the Search policy validationMode is ${JSON.stringify(facts.policyValidationMode)}, expected "FAIL_ON_ANY_FINDINGS"`);
  }

  if (facts.authorizerDiscoveryUrl !== facts.expectedDiscoveryUrl) {
    fail("the CUSTOM_JWT authorizer discoveryUrl is not the OIDC issuer's discovery URL");
  }

  if (!facts.authorizerAllowedAudience || !facts.authorizerAllowedAudience.includes(facts.expectedAudience)) {
    fail(`the CUSTOM_JWT authorizer allowedAudience does not contain ${JSON.stringify(facts.expectedAudience)}`);
  }

  if (facts.authorizerHasAllowedClients) {
    fail("the CUSTOM_JWT authorizer must not declare allowedClients");
  }

  if (facts.mcpHasSearchType) {
    fail("the Search_Gateway MCP protocol config must not declare searchType (semantic tool search is disabled)");
  }
}
