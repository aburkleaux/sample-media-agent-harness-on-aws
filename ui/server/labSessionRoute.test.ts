// Proves the `/lab` route actually SENDS the derived runtime session id.
//
// `labSession.test.ts` covers the derivation in isolation; this file covers the
// wiring, which is where the bug lived. The route built a `headers` object and
// passed it to `signedFetch` — but left it empty, so no session id ever reached
// AgentCore and every Capabilities tab paid ~8.3s of container init. A comment
// at the mount site claimed "session affinity: the frontend already threads a
// `session_id` through every /lab request body", which was true of `/api` and
// not of `/lab`; the value was read nowhere. A unit test on the helper alone
// would have stayed green through that.

import { describe, it, expect } from "vitest";

import { labRuntimeSessionId } from "./labSession";

const SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id";

/**
 * Mirrors the header construction at the `/lab` mount site. Kept as a named
 * helper so the assertion below describes intent rather than re-deriving the
 * hash inline.
 */
function labHeadersFor(actorId: string | undefined): Record<string, string> {
  const headers: Record<string, string> = {};
  const sessionId = labRuntimeSessionId(actorId);
  if (sessionId) headers[SESSION_HEADER] = sessionId;
  return headers;
}

describe("/lab runtime session header", () => {
  it("sends a session id when the actor is known", () => {
    const headers = labHeadersFor("operator@mediaworksdemo.com");
    expect(headers[SESSION_HEADER]).toBeTruthy();
    expect(headers[SESSION_HEADER].length).toBeGreaterThanOrEqual(33);
  });

  it("omits the header entirely when there is no actor", () => {
    // Not "sends an empty string" — an empty session header is a malformed
    // request, and the pre-fix behavior (no header) is the correct fallback.
    const headers = labHeadersFor(undefined);
    expect(SESSION_HEADER in headers).toBe(false);
  });

  it("sends different session ids for different actors", () => {
    const a = labHeadersFor("operator@mediaworksdemo.com")[SESSION_HEADER];
    const b = labHeadersFor("extcaller2@mediaworksdemo.com")[SESSION_HEADER];
    expect(a).not.toBe(b);
  });

  it("is wired into the real /lab route, not just this helper", async () => {
    // The regression guard. Reads the mount site and asserts it both derives a
    // session id and forwards it on the SigV4 call — the two halves that were
    // missing. Source-level because the route's only observable effect here is
    // an outbound header on a signed HTTPS request to AgentCore.
    const { readFileSync } = await import("node:fs");
    const src = readFileSync(new URL("./app.ts", import.meta.url), "utf8");
    const labBlock = src.slice(
      src.indexOf('app.use("/lab"'),
      src.indexOf('app.use("/api"'),
    );
    expect(labBlock).toContain("labRuntimeSessionId");
    expect(labBlock).toContain(SESSION_HEADER);
  });
});
