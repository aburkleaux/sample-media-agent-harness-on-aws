// /api/artifact must forward HTTP Range requests to S3 and answer `206 Partial Content`,
// so media is seekable through the proxy — including a non-faststart MP4 whose `moov` atom
// sits at the END of the file (the browser range-fetches the tail to start playback).
// Regression guard for the stitched-video playback investigation (session f485d134).
import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";

const TOTAL = 4867024;
let lastGetInput: any = null;

vi.mock("@aws-sdk/client-s3", () => {
  class GetObjectCommand { constructor(public input: any) {} }
  // Bodies are exactly ContentLength bytes. A body shorter than the declared length leaves the
  // client waiting for bytes that never come on a connection the server considers finished.
  const bytes = (n: number) => new Uint8Array(n);
  class S3Client {
    async send(cmd: any) {
      if (cmd instanceof GetObjectCommand) {
        lastGetInput = cmd.input;
        const range: string | undefined = cmd.input.Range;
        if (range) {
          const m = /bytes=(\d+)-(\d+)?/.exec(range);
          const start = m ? parseInt(m[1], 10) : 0;
          const end = m && m[2] ? parseInt(m[2], 10) : TOTAL - 1;
          return {
            ContentType: "video/mp4",
            ContentLength: end - start + 1,
            ContentRange: `bytes ${start}-${end}/${TOTAL}`,
            Body: { transformToByteArray: async () => bytes(end - start + 1) },
          };
        }
        return {
          ContentType: "video/mp4",
          ContentLength: TOTAL,
          Body: { transformToByteArray: async () => bytes(TOTAL) },
        };
      }
      return {};
    }
  }
  return {
    S3Client,
    GetObjectCommand,
    PutObjectCommand: class { constructor(public input: any) {} },
    ListObjectsV2Command: class { constructor(public input: any) {} },
    DeleteObjectCommand: class { constructor(public input: any) {} },
  };
});

import type http from "http";
import { buildApp } from "./app";
import { closeServers, listenLoopback } from "./testing/loopback";

const servers: http.Server[] = [];
const VARS = ["AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "USER_POOL_ISSUER", "USER_POOL_CLIENT_ID"];
const saved: Record<string, string | undefined> = {};

beforeEach(() => {
  lastGetInput = null;
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = "test-bucket";
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

describe("/api/artifact — HTTP Range support (seekable media)", () => {
  it("forwards Range to S3 and returns 206 Partial Content with Content-Range", async () => {
    const base = await listen();
    const r = await fetch(`${base}/api/artifact?key=artifacts/s/output/clip.mp4`, {
      headers: { Range: "bytes=0-99" },
    });
    expect(r.status).toBe(206);
    expect(r.headers.get("content-range")).toBe(`bytes 0-99/${TOTAL}`);
    expect(r.headers.get("accept-ranges")).toBe("bytes");
    expect(r.headers.get("content-type")).toBe("video/mp4");
    expect(r.headers.get("content-length")).toBe("100");
    expect((await r.arrayBuffer()).byteLength).toBe(100);
    // the Range header was actually forwarded to S3 (not dropped)
    expect(lastGetInput?.Range).toBe("bytes=0-99");
  });

  it("serves an open-ended range (bytes=N-) as 206", async () => {
    const base = await listen();
    const r = await fetch(`${base}/api/artifact?key=artifacts/s/output/clip.mp4`, {
      headers: { Range: "bytes=1000-" },
    });
    expect(r.status).toBe(206);
    expect(r.headers.get("content-range")).toBe(`bytes 1000-${TOTAL - 1}/${TOTAL}`);
    expect(lastGetInput?.Range).toBe("bytes=1000-");
    expect((await r.arrayBuffer()).byteLength).toBe(TOTAL - 1000);
  });

  it("advertises Accept-Ranges and does not forward a Range on a full (200) response", async () => {
    const base = await listen();
    const r = await fetch(`${base}/api/artifact?key=artifacts/s/output/clip.mp4`);
    expect(r.status).toBe(200);
    expect(r.headers.get("accept-ranges")).toBe("bytes");
    expect(r.headers.get("content-length")).toBe(String(TOTAL));
    expect((await r.arrayBuffer()).byteLength).toBe(TOTAL);
    expect(lastGetInput?.Range).toBeUndefined();
  });
});
