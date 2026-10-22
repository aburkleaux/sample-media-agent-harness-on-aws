// The proxy half of the ALB origin lock: with ORIGIN_VERIFY_SECRET set, every request except /health
// must carry our CloudFront distribution's X-Origin-Verify header — public routes, authed routes, the
// external /invoke surface and unknown paths alike.
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import http from "http";

import { buildApp } from "./app";
import { originVerifyMiddleware, ORIGIN_VERIFY_HEADER } from "./originVerify";
import { closeServers, listenLoopback } from "./testing/loopback";

const SECRET = "s3cr3t-value-from-secrets-manager-0123456789abcd";
const servers: http.Server[] = [];
const VARS = ["AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "ORIGIN_VERIFY_SECRET"];
const saved: Record<string, string | undefined> = {};

beforeEach(() => {
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = "test-bucket";
  process.env.DEV_BYPASS_AUTH = "true"; // even an "authenticated" caller is refused without the header
  process.env.ORIGIN_VERIFY_SECRET = SECRET;
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

const base = () => listenLoopback(buildApp(), servers);

/** Raw HTTP (not fetch: vitest.setup.ts answers any `/config` fetch itself, and fetch normalizes `..`). */
function raw(baseUrl: string, method: string, path: string, headers: Record<string, string> = {}):
    Promise<{ status: number; json: () => any }> {
  const u = new URL(baseUrl);
  return new Promise((resolve, reject) => {
    const req = http.request({ host: u.hostname, port: u.port, method, path, headers }, (res) => {
      const chunks: Buffer[] = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => {
        const text = Buffer.concat(chunks).toString();
        resolve({ status: res.statusCode ?? 0, json: () => JSON.parse(text) });
      });
    });
    req.on("error", reject);
    if (method === "POST") req.write("{}");
    req.end();
  });
}

describe("origin lock (X-Origin-Verify)", () => {
  const paths: [string, string][] = [
    ["GET", "/config"], ["GET", "/models"], ["POST", "/api/invocations"], ["POST", "/lab"],
    ["GET", "/api/artifact?key=artifacts/x/output/a.png"], ["POST", "/invoke/agent-1"],
    ["GET", "/sessions"], ["GET", "/no-such-route"], ["GET", "/health/../config"],
  ];

  it.each(paths)("refuses %s %s without the header (403, nothing served)", async (method, path) => {
    const r = await raw(await base(), method, path);
    expect(r.status).toBe(403);
    expect(r.json().error.code).toBe("origin_not_allowed");
  });

  it("refuses a wrong or near-miss header", async () => {
    const b = await base();
    for (const v of ["", "wrong", SECRET.slice(0, -1), `${SECRET}x`, SECRET.toUpperCase()]) {
      const r = await raw(b, "GET", "/config", { [ORIGIN_VERIFY_HEADER]: v });
      expect(r.status).toBe(403);
    }
  });

  it("serves a request that carries the header", async () => {
    const r = await raw(await base(), "GET", "/config", { [ORIGIN_VERIFY_HEADER]: SECRET });
    expect(r.status).toBe(200);
  });

  it("exempts only the ALB health check", async () => {
    const r = await raw(await base(), "GET", "/health");
    expect(r.status).toBe(200);
  });

  it("is a no-op where no secret is configured (local dev)", async () => {
    delete process.env.ORIGIN_VERIFY_SECRET;
    expect((await raw(await base(), "GET", "/config")).status).toBe(200);
  });

  it("is registered before every route", async () => {
    const fs = await import("fs");
    const path = await import("path");
    const src = fs.readFileSync(path.join(__dirname, "app.ts"), "utf8");
    const lock = src.indexOf("app.use(originVerifyMiddleware(");
    const firstRoute = src.search(/app\.use\(\s*["']\//);
    expect(lock).toBeGreaterThan(0);
    expect(lock).toBeLessThan(firstRoute);
  });

  it("middleware unit: constant-time compare semantics", () => {
    const mw = originVerifyMiddleware(SECRET);
    const run = (headers: Record<string, string>, url = "/x") => {
      let status = 0; let passed = false;
      mw({ url, headers }, { writeHead: (s: number) => { status = s; }, end: () => {} }, () => { passed = true; });
      return passed ? 200 : status;
    };
    expect(run({ [ORIGIN_VERIFY_HEADER]: SECRET })).toBe(200);
    expect(run({})).toBe(403);
    expect(run({}, "/health")).toBe(200);
    expect(run({}, "/health?x=1")).toBe(200);
    expect(run({}, "/healthz")).toBe(403);
  });
});
