/**
 * `POST /upload` must actually USE the session-scoped placement rule.
 *
 * `uploadKey.test.ts` pins the rule; this pins the WIRING, which is where the real bug was.
 * The correct placement already existed agent-side (`main.upload_file`) and in a client
 * helper (`api.uploadFile`, which sends `session_id`) — but nothing called either, so 269
 * uploads landed flat and session-less. A unit test on the rule would have stayed green
 * through all of that. So this drives the real Express route over real HTTP with real
 * multipart parsing, and only stubs S3.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";

// Stub S3 before importing app: the route PUTs the object and (in the no-CloudFront case)
// presigns a GET. Neither can reach AWS in a unit test.
const putKeys: string[] = [];
const completeKeys: string[] = [];
const abortKeys: string[] = [];
vi.mock("@aws-sdk/client-s3", () => {
  class PutObjectCommand { constructor(public input: any) {} }
  class GetObjectCommand { constructor(public input: any) {} }
  class CreateMultipartUploadCommand { constructor(public input: any) {} }
  class UploadPartCommand { constructor(public input: any) {} }
  class CompleteMultipartUploadCommand { constructor(public input: any) {} }
  class AbortMultipartUploadCommand { constructor(public input: any) {} }
  class S3Client {
    async send(cmd: any) {
      if (cmd instanceof PutObjectCommand) putKeys.push(cmd.input.Key);
      if (cmd instanceof CreateMultipartUploadCommand) return { UploadId: "test-upload-id" };
      if (cmd instanceof UploadPartCommand) return { ETag: `"etag-${cmd.input.PartNumber}"` };
      if (cmd instanceof CompleteMultipartUploadCommand) { completeKeys.push(cmd.input.Key); return {}; }
      if (cmd instanceof AbortMultipartUploadCommand) { abortKeys.push(cmd.input.Key); return {}; }
      return {};
    }
  }
  return {
    S3Client,
    PutObjectCommand,
    GetObjectCommand,
    CreateMultipartUploadCommand,
    UploadPartCommand,
    CompleteMultipartUploadCommand,
    AbortMultipartUploadCommand,
    ListObjectsV2Command: class { constructor(public input: any) {} },
    DeleteObjectCommand: class { constructor(public input: any) {} },
  };
});
vi.mock("@aws-sdk/s3-request-presigner", () => ({
  getSignedUrl: async () => "https://s3.example/presigned?X-Amz-Signature=deadbeef",
}));

// The route now VERIFIES that a client-supplied `session_id` belongs to the authenticated
// actor before scoping the key to it (an unauthenticated caller used to be able to write into
// any session's prefix). Model the sessions table so ownership can resolve; `sessionOwner`
// lets a test make the session belong to somebody else.
let sessionOwner: string | undefined = "local-dev-user";
vi.mock("@aws-sdk/lib-dynamodb", () => {
  class Cmd { constructor(public input: any) {} }
  return {
    DynamoDBDocumentClient: { from: () => ({
      async send(cmd: any) {
        // Only the sessions ownership GetItem matters here.
        if (cmd?.input?.Key?.sessionId !== undefined) {
          return sessionOwner === undefined ? {} : { Item: { actorId: sessionOwner } };
        }
        return {};
      },
    }) },
    GetCommand: class extends Cmd {},
    PutCommand: class extends Cmd {},
    UpdateCommand: class extends Cmd {},
    DeleteCommand: class extends Cmd {},
    QueryCommand: class extends Cmd {},
  };
});

import http from "http";
import { buildApp } from "./app";
import { artifactOwnerTag } from "./artifactOwner";

// A session-less upload lands under the UPLOADER's owner-tagged prefix, which the read policy serves
// to them alone (artifactOwner.ts). It used to be the shared `artifacts/uploads/input/`.
const OWN_UPLOADS = `artifacts/uploads/${artifactOwnerTag("local-dev-user")}/input/`;
import { closeServers, listenLoopback } from "./testing/loopback";

const servers: http.Server[] = [];
const VARS = ["AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "ARTIFACTS_DOMAIN", "UPLOAD_MAX_BYTES", "SESSIONS_TABLE", "LAB_DEFAULT_ACTOR_ID"];
const saved: Record<string, string | undefined> = {};

beforeEach(() => {
  putKeys.length = 0;
  completeKeys.length = 0;
  abortKeys.length = 0;
  sessionOwner = "local-dev-user";   // by default the session belongs to the caller
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = "test-bucket";
  process.env.DEV_BYPASS_AUTH = "true"; // /upload is gated; auth is not what's under test
  process.env.LAB_DEFAULT_ACTOR_ID = "local-dev-user";
  process.env.SESSIONS_TABLE = "test-sessions";
  process.env.ARTIFACTS_DOMAIN = "cdn.example.net";
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

function listen(): Promise<string> {
  return listenLoopback(buildApp(), servers);
}

/** Real multipart body, so busboy's field/file parsing is genuinely exercised. */
function multipart(fields: Record<string, string>, filename: string, content: string) {
  const b = "----vitestboundary";
  const parts = Object.entries(fields).map(
    ([k, v]) => `--${b}\r\nContent-Disposition: form-data; name="${k}"\r\n\r\n${v}\r\n`,
  );
  parts.push(
    `--${b}\r\nContent-Disposition: form-data; name="file"; filename="${filename}"\r\n` +
    `Content-Type: application/octet-stream\r\n\r\n${content}\r\n`,
  );
  return { body: Buffer.from(parts.join("") + `--${b}--\r\n`), contentType: `multipart/form-data; boundary=${b}` };
}

async function upload(base: string, fields: Record<string, string>, filename = "clip.mp4") {
  const { body, contentType } = multipart(fields, filename, "fake-bytes");
  const r = await fetch(`${base}/upload`, { method: "POST", headers: { "content-type": contentType }, body });
  return { status: r.status, json: await r.json() as any };
}

describe("POST /upload — session-scoped INPUT placement", () => {
  it("puts the object under artifacts/<session>/input/ when a session is sent", async () => {
    const { status, json } = await upload(await listen(), { session_id: "sess-xyz" });
    expect(status).toBe(200);
    expect(json.key).toMatch(/^artifacts\/sess-xyz\/input\/\d+-clip\.mp4$/);
    // The S3 write used that key — streamed via multipart, so it's the COMPLETED
    // upload's key (not a single PutObject) that must carry the placement.
    expect(completeKeys).toEqual([json.key]);
  });

  it("streams oversized files to a clean 413 and leaves no object behind", async () => {
    process.env.UPLOAD_MAX_BYTES = "4"; // "fake-bytes" (9 bytes) exceeds this
    const { status, json } = await upload(await listen(), { session_id: "big" });
    expect(status).toBe(413);
    expect(json.code).toBe("FILE_TOO_LARGE");
    expect(json.maxBytes).toBe(4);
    // Aborted, not completed — no partial object is left in the bucket.
    expect(completeKeys).toEqual([]);
    expect(abortKeys.length).toBe(1);
  });

  it("accepts the camelCase spelling too", async () => {
    const { json } = await upload(await listen(), { sessionId: "sess-camel" });
    expect(json.key).toContain("artifacts/sess-camel/input/");
  });

  it("still uploads with no session, under the uploader's own session-less input prefix", async () => {
    const { status, json } = await upload(await listen(), {});
    expect(status).toBe(200);
    expect(json.key.startsWith(OWN_UPLOADS)).toBe(true);
    expect(json.key.slice(OWN_UPLOADS.length)).toMatch(/^\d+-clip\.mp4$/);
  });

  it("returns the CloudFront url for the session-scoped key", async () => {
    const { json } = await upload(await listen(), { session_id: "s1" });
    expect(json.cloudfrontUrl).toBe(`https://cdn.example.net/${json.key}`);
  });

  it("does NOT presign when CloudFront serves the artifact", async () => {
    // A presigned URL for CloudFront-public content expires, bypasses the CDN, and its
    // X-Amz-* query params match redaction's credential-shape patterns.
    const { json } = await upload(await listen(), { session_id: "s1" });
    expect(json.url).toBe("");
  });

  it("DOES presign when there is no CloudFront domain (local dev keeps working)", async () => {
    delete process.env.ARTIFACTS_DOMAIN;
    const { json } = await upload(await listen(), { session_id: "s1" });
    expect(json.cloudfrontUrl).toBe("");
    expect(json.url).toContain("X-Amz-Signature");
  });

  it("cannot be made to escape the artifacts prefix via the session id", async () => {
    const { json } = await upload(await listen(), { session_id: "../../.." });
    expect(json.key.startsWith("artifacts/")).toBe(true);
    expect(json.key).not.toContain("..");
  });

  it("IGNORES a session id the caller does not own (no cross-tenant write)", async () => {
    // `session_id` is attacker-controlled request-body data. Someone else's session must never
    // scope the key, or a caller could plant content inside another user's session prefix.
    sessionOwner = "somebody-else";
    const { status, json } = await upload(await listen(), { session_id: "victim-session" });
    expect(status).toBe(200);
    expect(json.key).not.toContain("victim-session");
    expect(json.key.startsWith(OWN_UPLOADS)).toBe(true);
  });

  it("falls back to the session-less prefix when the session does not exist", async () => {
    sessionOwner = undefined;  // no such row (sessions are created lazily)
    const { status, json } = await upload(await listen(), { session_id: "not-persisted-yet" });
    expect(status).toBe(200);
    expect(json.key.startsWith(OWN_UPLOADS)).toBe(true);
  });
});
