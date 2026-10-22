// Bounded streamed S3 Multipart Upload for Slack file ingest. Isolated here (only
// @aws-sdk/client-s3 import, no relative imports) so it is unit-testable under
// Node's ESM test runner with a mock S3 client + mock download stream.
//
// Streams an authenticated download into S3 with a single part-buffer at a time
// (bounded memory), enforcing the actual per-file cap, remaining aggregate cap,
// and a preparation deadline — aborting the multipart upload (leaving NO
// completed object) on any breach.

import {
  S3Client,
  CreateMultipartUploadCommand,
  UploadPartCommand,
  CompleteMultipartUploadCommand,
  AbortMultipartUploadCommand,
} from "@aws-sdk/client-s3";

const PART_SIZE = 16 * 1024 * 1024; // 16 MiB; last part may be smaller.

export interface StreamToS3Options {
  bucket: string;
  key: string;
  contentType: string;
  url: string;
  botToken: string;
  perFileCap: number;
  aggRemaining: number;
  deadlineMs: number;
}

/** Returns the actual bytes written; throws (after aborting) on any breach. */
export async function streamToS3(s3: S3Client, o: StreamToS3Options): Promise<number> {
  const dl = await fetch(o.url, { headers: { authorization: `Bearer ${o.botToken}` } });
  if (!dl.ok || !dl.body) throw new Error(`download ${dl.status}`);

  const create = await s3.send(
    new CreateMultipartUploadCommand({ Bucket: o.bucket, Key: o.key, ContentType: o.contentType }),
  );
  const uploadId = create.UploadId as string;
  const parts: Array<{ PartNumber: number; ETag: string }> = [];
  let partNumber = 0;
  let total = 0;
  let pending: Buffer[] = [];
  let pendingLen = 0;

  const abort = async () => {
    try {
      await s3.send(new AbortMultipartUploadCommand({ Bucket: o.bucket, Key: o.key, UploadId: uploadId }));
    } catch {
      /* best effort */
    }
  };
  const flush = async () => {
    if (pendingLen === 0) return;
    const body = Buffer.concat(pending, pendingLen);
    pending = [];
    pendingLen = 0;
    partNumber += 1;
    const up = await s3.send(
      new UploadPartCommand({ Bucket: o.bucket, Key: o.key, UploadId: uploadId, PartNumber: partNumber, Body: body }),
    );
    parts.push({ PartNumber: partNumber, ETag: up.ETag as string });
  };

  try {
    const reader = (dl.body as any).getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = Buffer.from(value);
      total += chunk.length;
      if (total > o.perFileCap) throw new Error(`actual size exceeded per-file cap`);
      if (total > o.aggRemaining) throw new Error(`actual size exceeded aggregate cap`);
      if (Date.now() > o.deadlineMs) throw new Error(`deadline reached mid-transfer`);
      pending.push(chunk);
      pendingLen += chunk.length;
      if (pendingLen >= PART_SIZE) await flush();
    }
    await flush(); // final (possibly small) part
    if (parts.length === 0) throw new Error("empty file");
    await s3.send(
      new CompleteMultipartUploadCommand({
        Bucket: o.bucket,
        Key: o.key,
        UploadId: uploadId,
        MultipartUpload: { Parts: parts },
      }),
    );
    return total;
  } catch (err) {
    await abort(); // no completed object on any failure/breach
    throw err;
  }
}
