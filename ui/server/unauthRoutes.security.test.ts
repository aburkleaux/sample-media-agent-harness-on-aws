/**
 * The three routes that used to be reachable with NO credentials.
 *
 * Found by a security review of the internet-facing proxy:
 *   - `POST /upload`   streamed up to 2 GiB into the artifacts bucket and returned a permanent
 *                      PUBLIC CloudFront URL — anonymous content hosting + storage abuse — and
 *                      scoped the S3 key from a client-supplied `session_id`.
 *   - `POST /dispatch` SigV4-signed the caller's body with the PROXY's own AWS credentials and
 *                      forwarded it to the Task Agent runtime — anonymous, billable model/tool
 *                      execution (worse because `task_agent` treats a missing `tools` key as
 *                      "load every tool"). Gated, it was still a raw-body forward that let any
 *                      signed-in user pick the identity/config fields — it is now DELETED.
 *   - `GET  /models`   enumerated Bedrock inference profiles anonymously.
 *
 * All three are now in the proxy's `authPaths`, so the auth middleware short-circuits an
 * unattributed request with a 401 before any handler runs. `authPaths.contract.test.ts`
 * separately keeps the client's PROTECTED_PREFIXES in step, so the SPA still sends its token.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import http from "http";

// S3/DynamoDB are never reached (auth rejects first), but buildApp constructs the clients.
vi.mock("@aws-sdk/client-s3", () => ({
  S3Client: class { async send() { return {}; } },
  PutObjectCommand: class { constructor(public input: any) {} },
  GetObjectCommand: class { constructor(public input: any) {} },
  ListObjectsV2Command: class { constructor(public input: any) {} },
  DeleteObjectCommand: class { constructor(public input: any) {} },
  CreateMultipartUploadCommand: class { constructor(public input: any) {} },
  UploadPartCommand: class { constructor(public input: any) {} },
  CompleteMultipartUploadCommand: class { constructor(public input: any) {} },
  AbortMultipartUploadCommand: class { constructor(public input: any) {} },
}));

import { buildApp } from "./app";
import { closeServers, listenLoopback } from "./testing/loopback";

const servers: http.Server[] = [];
const VARS = [
  "AGENT_RUNTIME_ARN", "TASK_AGENT_RUNTIME_ARN", "UPLOAD_BUCKET",
  "DEV_BYPASS_AUTH", "USER_POOL_ISSUER", "USER_POOL_CLIENT_ID", "AWS_REGION",
];
const saved: Record<string, string | undefined> = {};

beforeEach(() => {
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.TASK_AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-BBBBBBBBBB";
  process.env.UPLOAD_BUCKET = "test-bucket";
  process.env.AWS_REGION = "us-west-2";
  // Real auth (no dev bypass) — this is exactly what a public internet caller faces.
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

describe("previously-unauthenticated proxy routes now require a token", () => {
  it("401s POST /upload with no Authorization header (no anonymous S3 write)", async () => {
    const base = await listen();
    const body = Buffer.from(
      '----b\r\nContent-Disposition: form-data; name="file"; filename="x.txt"\r\n\r\nhi\r\n----b--\r\n',
    );
    const r = await fetch(`${base}/upload`, {
      method: "POST",
      headers: { "content-type": "multipart/form-data; boundary=--b" },
      body,
    });
    expect(r.status).toBe(401);
  });

  it("401s GET /models with no Authorization header", async () => {
    const r = await fetch(`${await listen()}/models`);
    expect(r.status).toBe(401);
  });

  it("401s a forged/garbage bearer token rather than trusting it", async () => {
    const r = await fetch(`${await listen()}/models`, {
      headers: { authorization: "Bearer not-a-real-jwt" },
    });
    expect(r.status).toBe(401);
  });
});

describe("/dispatch is gone", () => {
  // It forwarded the caller's RAW body to the Task Agent signed with the proxy role, so even an
  // authenticated user chose actor_id / agent_id / agent_test / tools / s3_bucket / ddb_table /
  // plan_id / node_id. There is no safe form of it; the route must not exist at all.
  it("an AUTHENTICATED POST /dispatch reaches no runtime and is not served", async () => {
    process.env.DEV_BYPASS_AUTH = "true";
    const https = (await import("https")).default;
    const spy = vi.spyOn(https, "request");
    try {
      const r = await fetch(`${await listen()}/dispatch`, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: "Bearer x" },
        body: JSON.stringify({ prompt: "hello", actor_id: "victim", tools: ["*"] }),
      });
      expect(r.status).toBe(404);
      expect(spy).not.toHaveBeenCalled();
    } finally {
      spy.mockRestore();
    }
  });

  it("is registered nowhere: proxy authPaths, SPA PROTECTED_PREFIXES, CloudFront behaviors, SPA callers", async () => {
    const fs = await import("fs");
    const path = await import("path");
    const root = path.resolve(__dirname, "..", "..");
    const read = (p: string) => fs.readFileSync(path.join(root, p), "utf8");
    expect(read("ui/server/app.ts")).not.toMatch(/app\.use\(\s*["']\/dispatch["']/);
    expect(read("ui/server/app.ts")).not.toMatch(/["']\/dispatch["']\s*[,\]]/);
    expect(read("ui/src/auth/authFetch.ts")).not.toMatch(/["']\/dispatch["']/);
    expect(read("ui/src/api.ts")).not.toMatch(/["']\/dispatch["']/);
    expect(read("infra/lib/agent-infra-stack.ts")).not.toMatch(/["']\/dispatch["']\s*:/);
  });
});
