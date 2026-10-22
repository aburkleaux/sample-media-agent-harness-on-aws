// Validates the streamed multipart transfer logic with a MOCK S3 client + mock
// Slack download stream (no AWS, no network). Run: node --test fileIngest.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { streamToS3 } from "./multipart.ts";

const MiB = 1024 * 1024;

// Fake S3 client: records commands, returns plausible responses by command name.
function fakeS3() {
  const calls: string[] = [];
  const parts: any[] = [];
  return {
    calls,
    parts,
    async send(cmd: any) {
      const name = cmd.constructor.name;
      calls.push(name);
      if (name === "CreateMultipartUploadCommand") return { UploadId: "uid-1" };
      if (name === "UploadPartCommand") {
        parts.push({ n: cmd.input.PartNumber, len: cmd.input.Body.length });
        return { ETag: `"etag-${cmd.input.PartNumber}"` };
      }
      if (name === "CompleteMultipartUploadCommand") return {};
      if (name === "AbortMultipartUploadCommand") return {};
      throw new Error("unexpected command " + name);
    },
  } as any;
}

// Mock a download of `total` bytes delivered in `chunk`-sized reads.
function mockDownload(total: number, chunk = 4 * MiB) {
  return {
    ok: true,
    body: {
      getReader() {
        let sent = 0;
        return {
          async read() {
            if (sent >= total) return { done: true, value: undefined };
            const n = Math.min(chunk, total - sent);
            sent += n;
            return { done: false, value: new Uint8Array(n) };
          },
        };
      },
    },
  };
}

function withFetch(resp: any, fn: () => Promise<void>) {
  const orig = globalThis.fetch;
  (globalThis as any).fetch = async () => resp;
  return fn().finally(() => {
    (globalThis as any).fetch = orig;
  });
}

const baseOpts = {
  bucket: "b",
  key: "artifacts/slack-x/input/slack-F1-clip.mp4",
  contentType: "video/mp4",
  url: "https://files.slack.com/x",
  botToken: "test-bot-token",
  perFileCap: 1024 * MiB,
  aggRemaining: 2048 * MiB,
  deadlineMs: Date.now() + 600_000,
};

test("40 MiB streams as 3 parts (16+16+8) and completes", async () => {
  const s3 = fakeS3();
  await withFetch(mockDownload(40 * MiB), async () => {
    const bytes = await streamToS3(s3, baseOpts);
    assert.equal(bytes, 40 * MiB);
  });
  assert.equal(s3.parts.length, 3);
  assert.deepEqual(s3.parts.map((p: any) => p.len), [16 * MiB, 16 * MiB, 8 * MiB]);
  assert.ok(s3.calls.includes("CompleteMultipartUploadCommand"));
  assert.ok(!s3.calls.includes("AbortMultipartUploadCommand"));
});

test("small file (< part size) => one part, completes", async () => {
  const s3 = fakeS3();
  await withFetch(mockDownload(3 * MiB), async () => {
    const bytes = await streamToS3(s3, baseOpts);
    assert.equal(bytes, 3 * MiB);
  });
  assert.equal(s3.parts.length, 1);
  assert.ok(s3.calls.includes("CompleteMultipartUploadCommand"));
});

test("exceeding per-file cap aborts, no complete, throws", async () => {
  const s3 = fakeS3();
  await withFetch(mockDownload(30 * MiB), async () => {
    await assert.rejects(
      streamToS3(s3, { ...baseOpts, perFileCap: 10 * MiB }),
      /per-file cap/,
    );
  });
  assert.ok(s3.calls.includes("AbortMultipartUploadCommand"));
  assert.ok(!s3.calls.includes("CompleteMultipartUploadCommand"));
});

test("exceeding aggregate remaining aborts", async () => {
  const s3 = fakeS3();
  await withFetch(mockDownload(30 * MiB), async () => {
    await assert.rejects(streamToS3(s3, { ...baseOpts, aggRemaining: 10 * MiB }), /aggregate cap/);
  });
  assert.ok(s3.calls.includes("AbortMultipartUploadCommand"));
});

test("past deadline aborts mid-transfer", async () => {
  const s3 = fakeS3();
  await withFetch(mockDownload(30 * MiB), async () => {
    await assert.rejects(streamToS3(s3, { ...baseOpts, deadlineMs: Date.now() - 1 }), /deadline/);
  });
  assert.ok(s3.calls.includes("AbortMultipartUploadCommand"));
});

test("download failure throws before any multipart create", async () => {
  const s3 = fakeS3();
  await withFetch({ ok: false, status: 403, body: null }, async () => {
    await assert.rejects(streamToS3(s3, baseOpts), /download 403/);
  });
  assert.equal(s3.calls.length, 0);
});
