// /api/artifact is a PUBLIC content read-through (artifact content is already public via
// CloudFront). It must be reachable by a plain browser navigation (Open/Download links) that
// cannot carry an Authorization header — i.e. NOT behind the JWT auth middleware — while an
// ordinary /api path stays gated. It must also refuse to serve anything outside `artifacts/`.
import { describe, it, expect, afterEach, beforeEach } from "vitest";
import http from "http";
import { buildApp } from "./app";
import { closeServers, listenLoopback } from "./testing/loopback";

const servers: http.Server[] = [];
const saved: Record<string, string | undefined> = {};
const VARS = ["AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "USER_POOL_ISSUER", "USER_POOL_CLIENT_ID"];

beforeEach(() => {
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = "test-bucket";
  // Force REAL auth on, so a gated path 401s and the exemption is meaningful.
  process.env.DEV_BYPASS_AUTH = "false";
  process.env.USER_POOL_ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_test";
  process.env.USER_POOL_CLIENT_ID = "testclient";
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

function listen(): Promise<string> {
  return listenLoopback(buildApp(), servers);
}

describe("/api/artifact — public, key-validated read-through", () => {
  it("a gated /api path still 401s without a token (auth is really on)", async () => {
    const base = await listen();
    const r = await fetch(`${base}/api/does-not-exist`);
    expect(r.status).toBe(401);
  });

  it("/api/artifact is NOT auth-gated and REFUSES a non-artifact key (no token)", async () => {
    const base = await listen();
    const r = await fetch(`${base}/api/artifact?key=secrets/creds.json`);
    // 400, not 403: CloudFront rewrites 403 into 200 /index.html (see the CloudFront section
    // below). What matters here is that key validation ran at all — i.e. we reached the handler
    // rather than being 401'd by the auth middleware.
    expect(r.status).toBe(400);
    expect(r.status).not.toBe(401);
  });

  it("/api/artifact reaches S3 for an artifacts/ key without a token (never 401)", async () => {
    const base = await listen();
    const r = await fetch(`${base}/api/artifact?key=artifacts/nope/missing.pdf`);
    expect(r.status).not.toBe(401);      // auth-exempt; a missing object is 404/500, never 401
  });

  // The exemption was matched by PREFIX, so it silently covered every path merely STARTING with
  // `/api/artifact`. Such a path does 401 today — but from the `/api` catch-all's own defensive
  // "Authorization header is required" check, NOT from the auth middleware, which had already
  // waved it through. That distinction is the whole point: the moment someone registers a real
  // route (`/api/artifact-registry`, `/api/artifacts`) BEFORE that catch-all, it is genuinely
  // public, with nothing in the diff to suggest auth was involved.
  //
  // So assert WHICH layer gated it. The two 401s carry different messages, and only the
  // middleware's proves the exemption stopped at a path boundary.
  const MIDDLEWARE_401 = "Authorization header is required";
  it("the AUTH MIDDLEWARE gates a longer path that merely starts with /api/artifact", async () => {
    const base = await listen();
    for (const p of ["/api/artifactsomething", "/api/artifact-registry", "/api/artifacts"]) {
      const r = await fetch(`${base}${p}`);
      expect(r.status, `${p} must be 401`).toBe(401);
      const body = await r.json() as any;
      expect(body?.error?.message, `${p} must be gated by the middleware, not the catch-all`)
        .toBe(MIDDLEWARE_401);
    }
  });

  it("still exempts the real path with a subpath or query", async () => {
    const base = await listen();
    // A `/`-delimited subpath is the same resource, so the boundary rule must allow it.
    const r = await fetch(`${base}/api/artifact/?key=artifacts/nope/missing.pdf`);
    expect(r.status).not.toBe(401);
  });

  // ---------------------------------------------------------------- CloudFront error mapping
  // The UI distribution maps 403 -> 200 /index.html AND 404 -> 200 /index.html, distribution-wide
  // (CustomErrorResponses is not per-behaviour), cached 300s. So ANY 403/404 this route returns is
  // rewritten into a 200 serving the SPA's own HTML.
  //
  // Verified live on staging: `/api/artifact?key=artifacts/does-not-exist/render/nope.json`
  // returned `200 text/html` with the harness page body. That is precisely the reported
  // "the document link is wrong and the render ends up rendering the harness UI" symptom
  // (session ee8b405b) — `toProxySrc` fails closed on an unrecognisable URL, but this URL is
  // perfectly well-formed; it is the RESPONSE that lies, so nothing client-side can catch it.
  //
  // Fix: return statuses CloudFront does not rewrite. 400 for a key we refuse, 410 for an object
  // that is not there. Both are semantically defensible and neither is in the error map.
  it("refuses a non-artifact key with 400, not 403 (403 becomes 200 index.html at CloudFront)", async () => {
    const base = await listen();
    const r = await fetch(`${base}/api/artifact?key=secrets/creds.json`);
    expect(r.status, "403 is rewritten to 200 /index.html by CloudFront").toBe(400);
    expect(r.headers.get("content-type")).toContain("application/json");
    const body = await r.json() as any;
    expect(String(body?.error ?? "")).toContain("artifacts/");
  });

  it("reports a missing artifact as 410, not 404 (404 becomes 200 index.html at CloudFront)", async () => {
    const base = await listen();
    const r = await fetch(`${base}/api/artifact?key=artifacts/nope/missing.pdf`);
    // 500 would mean no credentials in this environment; only assert when S3 really answered.
    if (r.status === 500) return;
    expect(r.status, "404 is rewritten to 200 /index.html by CloudFront").toBe(410);
    expect(r.headers.get("content-type")).toContain("application/json");
  });

  it("never answers an artifact request with HTML", async () => {
    // The invariant that matters, independent of the exact code: a JSON API must not hand back
    // the SPA. An iframe pointed at this would render the harness inside itself.
    const base = await listen();
    for (const k of ["secrets/creds.json", "artifacts/nope/missing.pdf", ""]) {
      const r = await fetch(`${base}/api/artifact?key=${encodeURIComponent(k)}`);
      expect(r.headers.get("content-type") ?? "", `key=${k}`).not.toContain("text/html");
    }
  });
});
