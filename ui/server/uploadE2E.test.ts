/**
 * END-TO-END upload: real Express route, real multipart, REAL S3, real CloudFront.
 *
 * `uploadKey.test.ts` pins the placement rule and `uploadRoute.test.ts` pins the wiring, but both
 * stub S3 — so nothing proved that an upload actually LANDS at
 * `artifacts/<session>/input/<ts>-<name>` and is then fetchable over CloudFront. That round trip
 * is the whole point of the change (it is what makes an artifact's role derivable from its path,
 * and what the agent-side render harness then consumes), so it deserves a real check.
 *
 * OPT-IN: skipped unless `E2E_REAL_S3=1`, because it needs AWS credentials and writes an object.
 *
 *   E2E_REAL_S3=1 npx vitest run server/uploadE2E.test.ts
 *
 * Writes exactly one ~20-byte object under an `artifacts/e2e-upload-<ts>/input/` prefix and
 * deletes it again. Never touches anything else.
 *
 * (Do not write a glob with a star-slash in this comment — it closes the block early and the rest
 *  of the docblock is then parsed as code, which fails on the next template literal.)
 */
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import http from "http";
import { listenLoopback } from "./testing/loopback";
import { S3Client, DeleteObjectCommand, HeadObjectCommand } from "@aws-sdk/client-s3";

const ENABLED = process.env.E2E_REAL_S3 === "1";
const BUCKET = process.env.E2E_UPLOAD_BUCKET
  || "mediaworks-harness-skills-store-100000000002-us-west-2";
const DOMAIN = process.env.E2E_ARTIFACTS_DOMAIN || "dexamplecdn002.cloudfront.net";
const REGION = process.env.AWS_REGION || "us-west-2";
// A distinctive session id so anything left behind is obviously this test's.
const SESSION = `e2e-upload-${Math.floor(Date.now() / 1000)}`;

let base = "";
let server: http.Server | undefined;
let uploadedKey = "";
const s3 = new S3Client({ region: REGION });

beforeAll(async () => {
  if (!ENABLED) return;
  process.env.AGENT_RUNTIME_ARN =
    "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = BUCKET;
  process.env.ARTIFACTS_DOMAIN = DOMAIN;
  process.env.DEV_BYPASS_AUTH = "true"; // auth is covered elsewhere; not what this proves
  const { buildApp } = await import("./app");
  server = http.createServer(buildApp());
  base = await listenLoopback(server);
});

afterAll(async () => {
  server?.close();
  if (uploadedKey) {
    // Clean up after ourselves: this is the only object the test created.
    await s3.send(new DeleteObjectCommand({ Bucket: BUCKET, Key: uploadedKey })).catch(() => {});
  }
});

function multipart(sessionId: string, filename: string, content: string) {
  const b = "----e2euploadboundary";
  const body = Buffer.from(
    `--${b}\r\nContent-Disposition: form-data; name="session_id"\r\n\r\n${sessionId}\r\n` +
    `--${b}\r\nContent-Disposition: form-data; name="file"; filename="${filename}"\r\n` +
    `Content-Type: text/plain\r\n\r\n${content}\r\n` +
    `--${b}--\r\n`,
  );
  return { body, contentType: `multipart/form-data; boundary=${b}` };
}

describe.skipIf(!ENABLED)("POST /upload — real S3 + CloudFront round trip", () => {
  it("lands the object at artifacts/<session>/input/ and serves it over CloudFront", async () => {
    const { body, contentType } = multipart(SESSION, "e2e-probe.txt", "hello-from-e2e\n");
    const res = await fetch(`${base}/upload`, {
      method: "POST", headers: { "content-type": contentType }, body,
    });
    expect(res.status).toBe(200);
    const json = await res.json() as any;
    uploadedKey = json.key;

    // 1. the PLACEMENT — the property everything else derives from
    expect(json.key).toMatch(new RegExp(`^artifacts/${SESSION}/input/\\d+-e2e-probe\\.txt$`));

    // 2. the object REALLY exists in S3 (the write was not merely reported)
    const head = await s3.send(new HeadObjectCommand({ Bucket: BUCKET, Key: json.key }));
    expect(head.ContentLength).toBe(15);

    // 3. it is reachable over CloudFront at the url the agent is handed. This is the answer to
    //    "does the input land somewhere with a CloudFront url" — and CloudFront needs a moment
    //    for a brand-new object, so allow a few attempts before calling it a failure.
    expect(json.cloudfrontUrl).toBe(`https://${DOMAIN}/${json.key}`);
    let status = 0;
    for (let i = 0; i < 6; i++) {
      status = (await fetch(json.cloudfrontUrl)).status;
      if (status === 200) break;
      await new Promise(r => setTimeout(r, 2000));
    }
    expect(status, `CloudFront did not serve ${json.cloudfrontUrl}`).toBe(200);

    // 4. NO presigned url, because CloudFront serves it. A presign here would expire, bypass the
    //    CDN, and its X-Amz-* params match redaction's credential-shape patterns.
    expect(json.url).toBe("");
  }, 60_000);

  it("the key marks the object as a SOURCE by its path", async () => {
    // The structural property the agent side relies on (`provider._has_input_path`).
    expect(uploadedKey).toContain("/input/");
    expect(uploadedKey).not.toContain("/output/");
  });
});
