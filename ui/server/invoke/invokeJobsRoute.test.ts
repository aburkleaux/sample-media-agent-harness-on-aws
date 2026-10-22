// Integration tests for the external jobs read endpoint
// (GET /invoke/:agentId/jobs) — long-running workflow tracking.
//
// Mounts the REAL handler produced by `createInvokeJobsHandler` on a throwaway
// Express app + HTTP server and drives it over the wire with `fetch`. The
// AWS-touching deps (AssetIndex resolver, per-agent OIDC verifier, JobRegistry
// query) are injected as fakes so the tests are deterministic and hit no AWS.
//
// Coverage:
//   * valid token → returns ONLY jobs matching (owner actorId, derived session);
//   * a job with a different sessionId (another caller) is excluded;
//   * a job under a different actor (leaked into the query) is excluded;
//   * missing / invalid token → 401;
//   * disabled / revoked / unknown agent → 404 (no existence leak);
//   * misconfiguration → 500.

import { describe, it, expect, afterEach } from "vitest";
import { LOOPBACK_HOST } from "../testing/loopback";
import express from "express";
import http from "http";
import type { AddressInfo } from "net";

import { AgentTokenError } from "../auth/agentOidc";
import { deriveExternalSessionId } from "./externalSessionKey";
import {
  createInvokeJobsHandler,
  type InvokeJobsHandlerDeps,
} from "./invokeJobsRoute";

const ISSUER = "https://issuer.example.com";
const AUDIENCE = "urn:my-agent";
const OWNER = "owner-user-123";
const CALLER_SUB = "caller-sub-789";
const OTHER_SUB = "other-caller-456";
const AGENT_ID = "agent-abc";

const CALLER_SESSION = deriveExternalSessionId(AGENT_ID, ISSUER, CALLER_SUB);
const OTHER_SESSION = deriveExternalSessionId(AGENT_ID, ISSUER, OTHER_SUB);

function makeAgentRow(overrides: Record<string, any> = {}): Record<string, any> {
  return {
    ownerUserId: OWNER,
    version: 4,
    integrationConfig: {
      enabled: true,
      issuer: ISSUER,
      audience: AUDIENCE,
      allowedClientIds: [],
    },
    ...overrides,
  };
}

function jobRow(overrides: Record<string, any> = {}): Record<string, any> {
  return {
    jobId: "job-1",
    actorId: OWNER,
    sessionId: CALLER_SESSION,
    trioId: "media-processing",
    gatewayId: "__aws_sfn__",
    status: "RUNNING",
    startAt: "2026-07-24T00:00:00.000Z",
    updatedAt: "2026-07-24T00:00:01.000Z",
    terminalAt: null,
    lastError: null,
    ...overrides,
  };
}

/** Build a test server mounting the real handler with the supplied deps. */
async function mountHandler(deps: Partial<InvokeJobsHandlerDeps> = {}) {
  const full: InvokeJobsHandlerDeps = {
    assetIndexReady: true,
    jobRegistryReady: true,
    resolveCurrentPublishedAgent: async () => makeAgentRow(),
    verifyAgentToken: async () => ({ sub: CALLER_SUB, clientId: null }),
    queryActorJobs: async () => [],
    ...deps,
  };
  const app = express();
  app.get("/invoke/:agentId/jobs", createInvokeJobsHandler(full));
  const server = http.createServer(app);
  // Loopback, not the dual-stack default: see ../testing/loopback.ts.
  await new Promise<void>((r) => server.listen(0, LOOPBACK_HOST, r));
  const port = (server.address() as AddressInfo).port;
  return {
    url: `http://127.0.0.1:${port}/invoke/${AGENT_ID}/jobs`,
    close: () => new Promise<void>((r) => { server.closeAllConnections(); server.close(() => r()); }),
  };
}

const servers: Array<() => Promise<void>> = [];
afterEach(async () => {
  while (servers.length) await servers.pop()!();
});

async function get(url: string, headers: Record<string, string> = {}) {
  return fetch(url, { headers });
}

describe("GET /invoke/:agentId/jobs — external workflow tracking", () => {
  it("returns only the caller's own jobs (owner actor + derived session)", async () => {
    const { url, close } = await mountHandler({
      queryActorJobs: async () => [
        jobRow({ jobId: "mine-1", sessionId: CALLER_SESSION }),
        jobRow({ jobId: "other-caller", sessionId: OTHER_SESSION }),
        jobRow({ jobId: "mine-2", sessionId: CALLER_SESSION, status: "SUCCEEDED" }),
      ],
    });
    servers.push(close);

    const res = await get(url, { authorization: "Bearer tok" });
    expect(res.status).toBe(200);
    const body = await res.json();
    const ids = body.jobs.map((j: any) => j.jobId).sort();
    expect(ids).toEqual(["mine-1", "mine-2"]);
    // Never leaks another caller's job.
    expect(ids).not.toContain("other-caller");
  });

  it("excludes rows whose actorId differs from the resolved owner", async () => {
    const { url, close } = await mountHandler({
      queryActorJobs: async () => [
        jobRow({ jobId: "mine", sessionId: CALLER_SESSION }),
        // Defense-in-depth: a row that slipped through with a foreign actorId.
        jobRow({ jobId: "foreign-actor", actorId: "someone-else", sessionId: CALLER_SESSION }),
      ],
    });
    servers.push(close);

    const res = await get(url, { authorization: "Bearer tok" });
    const body = await res.json();
    expect(body.jobs.map((j: any) => j.jobId)).toEqual(["mine"]);
  });

  it("projects the wire-format Job shape", async () => {
    const { url, close } = await mountHandler({
      queryActorJobs: async () => [jobRow({ jobId: "j" })],
    });
    servers.push(close);
    const body = await (await get(url, { authorization: "Bearer tok" })).json();
    // Full wire shape from the SHARED `projectJob` — the internal (Cognito)
    // and external (OIDC) realms return the same `Job`, so the plan-execution
    // fields staging added surface here too rather than being dropped.
    expect(body.jobs[0]).toEqual({
      jobId: "j",
      actorId: OWNER,
      sessionId: CALLER_SESSION,
      trioId: "media-processing",
      gatewayId: "__aws_sfn__",
      status: "RUNNING",
      startAt: "2026-07-24T00:00:00.000Z",
      updatedAt: "2026-07-24T00:00:01.000Z",
      terminalAt: null,
      hasRenderableArtifacts: false,
      lastError: null,
      runId: null,
      turnId: null,
      nodeId: null,
      dependsOn: [],
      artifactUrls: [],
    });
  });

  it("enriches a SUCCEEDED SFN job with outputs read directly from Step Functions", async () => {
    const arn =
      "arn:aws:states:us-west-2:100000000001:execution:mediaworks-media-processing:1183efd0";
    const describeExecution = async (executionArn) => {
      expect(executionArn).toBe(arn);
      return {
        status: "SUCCEEDED",
        output: JSON.stringify({
          output: { s3uri: "s3://proxy-bucket/micro-workflow/media-processing/1183efd0" },
          input: { s3uri: "s3://src/artifacts/src.mp4" },
          data: {
            output_keys: {
              proxy: "micro-workflow/media-processing/1183efd0/proxy/transcoded.mp4",
            },
          },
        }),
      };
    };
    const { url, close } = await mountHandler({
      queryActorJobs: async () => [
        jobRow({ jobId: arn, gatewayId: "__aws_sfn__", status: "SUCCEEDED", sessionId: CALLER_SESSION }),
      ],
      describeExecution,
    });
    servers.push(close);

    const body = await (await get(url, { authorization: "Bearer tok" })).json();
    expect(body.jobs).toHaveLength(1);
    expect(body.jobs[0].output.outputPrefix).toContain("media-processing/1183efd0");
    expect(body.jobs[0].output.artifacts).toEqual([
      { label: "proxy", uri: "s3://proxy-bucket/micro-workflow/media-processing/1183efd0/proxy/transcoded.mp4" },
    ]);
  });

  it("does NOT describe active jobs and survives a describe failure", async () => {
    let describeCalls = 0;
    const { url, close } = await mountHandler({
      queryActorJobs: async () => [
        jobRow({ jobId: "arn:aws:states:us-west-2:1:execution:m:running", gatewayId: "__aws_sfn__", status: "RUNNING" }),
        jobRow({ jobId: "arn:aws:states:us-west-2:1:execution:m:boom", gatewayId: "__aws_sfn__", status: "SUCCEEDED" }),
      ],
      describeExecution: async () => { describeCalls++; throw new Error("sfn down"); },
    });
    servers.push(close);

    const res = await get(url, { authorization: "Bearer tok" });
    expect(res.status).toBe(200);
    const body = await res.json();
    // Only the SUCCEEDED job is described (RUNNING is skipped), and a describe
    // failure leaves that job present with no output rather than 500-ing.
    expect(describeCalls).toBe(1);
    expect(body.jobs).toHaveLength(2);
    expect(body.jobs.every((j) => j.output === undefined)).toBe(true);
  });

  it("401s when the Authorization header is missing", async () => {
    const { url, close } = await mountHandler();
    servers.push(close);
    const res = await get(url);
    expect(res.status).toBe(401);
    const body = await res.json();
    expect(body.error.category).toBe("missing_token");
  });

  it("401s when the token fails verification", async () => {
    const { url, close } = await mountHandler({
      verifyAgentToken: async () => {
        throw new AgentTokenError("invalid_token", "bad signature");
      },
    });
    servers.push(close);
    const res = await get(url, { authorization: "Bearer tok" });
    expect(res.status).toBe(401);
    expect((await res.json()).error.category).toBe("invalid_token");
  });

  it("403s when client_id is not in allowed_client_ids", async () => {
    const { url, close } = await mountHandler({
      resolveCurrentPublishedAgent: async () =>
        makeAgentRow({
          integrationConfig: {
            enabled: true,
            issuer: ISSUER,
            audience: AUDIENCE,
            allowedClientIds: ["allowed-client"],
          },
        }),
      verifyAgentToken: async () => ({ sub: CALLER_SUB, clientId: "other-client" }),
    });
    servers.push(close);
    const res = await get(url, { authorization: "Bearer tok" });
    expect(res.status).toBe(403);
    expect((await res.json()).error.category).toBe("client_not_allowed");
  });

  it("404s for an unknown agent (no existence leak)", async () => {
    const { url, close } = await mountHandler({
      resolveCurrentPublishedAgent: async () => null,
    });
    servers.push(close);
    const res = await get(url, { authorization: "Bearer tok" });
    expect(res.status).toBe(404);
    expect((await res.json()).error.category).toBe("not_found");
  });

  it("404s when external access is revoked even if the snapshot says enabled", async () => {
    const { url, close } = await mountHandler({
      resolveCurrentPublishedAgent: async () =>
        makeAgentRow({ externalAccessEnabled: false }),
    });
    servers.push(close);
    const res = await get(url, { authorization: "Bearer tok" });
    expect(res.status).toBe(404);
  });

  it("500s when the job registry is not configured", async () => {
    const { url, close } = await mountHandler({ jobRegistryReady: false });
    servers.push(close);
    const res = await get(url, { authorization: "Bearer tok" });
    expect(res.status).toBe(500);
    expect((await res.json()).error.category).toBe("internal_error");
  });
});
