// Integration tests for the external media upload endpoint
// (POST /invoke/:agentId/uploads) — presigned-PUT issuance.

import { describe, it, expect, afterEach } from "vitest";
import { LOOPBACK_HOST } from "../testing/loopback";
import express from "express";
import http from "http";
import type { AddressInfo } from "net";

import { AgentTokenError } from "../auth/agentOidc";
import {
  createInvokeUploadHandler,
  sanitizeFilename,
  type InvokeUploadHandlerDeps,
} from "./invokeUploadRoute";
import { externalArtifactOwnerTag } from "../artifactOwner";

const ISSUER = "https://issuer.example.com";
const AUDIENCE = "urn:my-agent";
const OWNER = "owner-user-123";
const CALLER_SUB = "caller-sub-789";
const AGENT_ID = "agent-abc";
const BUCKET = "mediaworks-harness-skills-store-x-y";
// The caller's own upload prefix: tagged per (agent, issuer, sub), so GET /invoke/:agentId/download
// serves it to this caller and to nobody else (artifactOwner.ts).
const PREFIX = `artifacts/uploads/${externalArtifactOwnerTag(AGENT_ID, ISSUER, CALLER_SUB)}/input`;

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

async function mountHandler(deps: Partial<InvokeUploadHandlerDeps> = {}) {
  const full: InvokeUploadHandlerDeps = {
    assetIndexReady: true,
    uploadReady: true,
    uploadBucket: BUCKET,
    resolveCurrentPublishedAgent: async () => makeAgentRow(),
    verifyAgentToken: async () => ({ sub: CALLER_SUB, clientId: null }),
    presignPut: async (key) => `https://s3.example/${key}?sig=abc`,
    now: () => 1784754468569,
    ...deps,
  };
  const app = express();
  app.post("/invoke/:agentId/uploads", createInvokeUploadHandler(full));
  const server = http.createServer(app);
  // Loopback, not the dual-stack default: see ../testing/loopback.ts.
  await new Promise<void>((r) => server.listen(0, LOOPBACK_HOST, r));
  const port = (server.address() as AddressInfo).port;
  return {
    url: `http://127.0.0.1:${port}/invoke/${AGENT_ID}/uploads`,
    close: () => new Promise<void>((r) => { server.closeAllConnections(); server.close(() => r()); }),
  };
}

const servers: Array<() => Promise<void>> = [];
afterEach(async () => {
  while (servers.length) await servers.pop()!();
});

function post(url: string, body: any, headers: Record<string, string> = {}) {
  return fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

describe("sanitizeFilename", () => {
  it("strips path components and disallowed chars", () => {
    expect(sanitizeFilename("../../etc/passwd")).toBe("passwd");
    expect(sanitizeFilename("my clip (final).mp4")).toBe("my_clip_final_.mp4");
    expect(sanitizeFilename("a/b\\c.mov")).toBe("c.mov");
  });
  it("drops leading dots and empty → default", () => {
    expect(sanitizeFilename("...hidden")).toBe("hidden");
    expect(sanitizeFilename("")).toBe("upload.bin");
    expect(sanitizeFilename(undefined)).toBe("upload.bin");
  });
});

describe("POST /invoke/:agentId/uploads", () => {
  // The key comes from the shared `uploadKeyFor` rule (server/uploadKey.ts), session-less
  // because an external caller uploads BEFORE it invokes. The `/input/` segment is the point:
  // it marks the object as a SOURCE by its path, so source detection no longer has to depend
  // on the `s3://` scheme (see agent/tests/test_source_role_from_path.py).
  it("returns a presigned PUT + canonical s3 URI for the artifacts/ input key", async () => {
    const { url, close } = await mountHandler();
    servers.push(close);
    const res = await post(url, { filename: "clip.mp4", contentType: "video/mp4" }, {
      authorization: "Bearer tok",
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.key).toBe(`${PREFIX}/1784754468569-clip.mp4`);
    expect(body.s3Uri).toBe(`s3://${BUCKET}/${PREFIX}/1784754468569-clip.mp4`);
    expect(body.bucket).toBe(BUCKET);
    expect(body.putUrl).toContain(`${PREFIX}/1784754468569-clip.mp4`);
    expect(body.expiresIn).toBe(900);
  });

  it("tags the key with the CALLER: another sub of the same agent gets a different prefix", async () => {
    const { url, close } = await mountHandler({ verifyAgentToken: async () => ({ sub: "another-sub", clientId: null }) });
    servers.push(close);
    const res = await post(url, { filename: "clip.mp4" }, { authorization: "Bearer tok" });
    const body = await res.json();
    expect(body.key).toBe(`artifacts/uploads/${externalArtifactOwnerTag(AGENT_ID, ISSUER, "another-sub")}/input/1784754468569-clip.mp4`);
    expect(body.key.startsWith(PREFIX)).toBe(false);
  });

  it("passes the sanitized filename + content-type into the presign key", async () => {
    let seenKey = "";
    let seenType = "";
    const { url, close } = await mountHandler({
      presignPut: async (key, contentType) => {
        seenKey = key;
        seenType = contentType;
        return "https://s3.example/put";
      },
    });
    servers.push(close);
    await post(url, { filename: "../secret movie.MP4", contentType: "video/mp4" }, {
      authorization: "Bearer tok",
    });
    expect(seenKey).toBe(`${PREFIX}/1784754468569-secret_movie.MP4`);
    expect(seenType).toBe("video/mp4");
  });

  it("defaults content-type when omitted", async () => {
    let seenType = "";
    const { url, close } = await mountHandler({
      presignPut: async (_k, t) => { seenType = t; return "u"; },
    });
    servers.push(close);
    await post(url, { filename: "a.bin" }, { authorization: "Bearer tok" });
    expect(seenType).toBe("application/octet-stream");
  });

  it("401s without a token", async () => {
    const { url, close } = await mountHandler();
    servers.push(close);
    const res = await post(url, { filename: "a.mp4" });
    expect(res.status).toBe(401);
    expect((await res.json()).error.category).toBe("missing_token");
  });

  it("401s when verification fails", async () => {
    const { url, close } = await mountHandler({
      verifyAgentToken: async () => { throw new AgentTokenError("expired", "tok expired"); },
    });
    servers.push(close);
    const res = await post(url, { filename: "a.mp4" }, { authorization: "Bearer tok" });
    expect(res.status).toBe(401);
    expect((await res.json()).error.category).toBe("expired");
  });

  it("403s when client_id is not allowed", async () => {
    const { url, close } = await mountHandler({
      resolveCurrentPublishedAgent: async () =>
        makeAgentRow({
          integrationConfig: {
            enabled: true, issuer: ISSUER, audience: AUDIENCE,
            allowedClientIds: ["good"],
          },
        }),
      verifyAgentToken: async () => ({ sub: CALLER_SUB, clientId: "bad" }),
    });
    servers.push(close);
    const res = await post(url, { filename: "a.mp4" }, { authorization: "Bearer tok" });
    expect(res.status).toBe(403);
  });

  it("404s for unknown / revoked agent", async () => {
    const { url, close } = await mountHandler({
      resolveCurrentPublishedAgent: async () => makeAgentRow({ externalAccessEnabled: false }),
    });
    servers.push(close);
    const res = await post(url, { filename: "a.mp4" }, { authorization: "Bearer tok" });
    expect(res.status).toBe(404);
  });

  it("500s when the upload bucket is not configured", async () => {
    const { url, close } = await mountHandler({ uploadReady: false, uploadBucket: "" });
    servers.push(close);
    const res = await post(url, { filename: "a.mp4" }, { authorization: "Bearer tok" });
    expect(res.status).toBe(500);
  });
});
