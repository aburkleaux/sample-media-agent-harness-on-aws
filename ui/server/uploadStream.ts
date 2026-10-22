// Bounded streamed S3 Multipart Upload for the browser `/upload` route.
//
// The route used to buffer the ENTIRE file into memory (`Buffer.concat(chunks)`)
// and then do one `PutObjectCommand`. For a large media upload that:
//   - ballooned the shared Fargate proxy's RAM (Buffer.concat is transiently ~2x),
//     risking an OOM kill of a task every browser session depends on;
//   - hit Node's ~2 GiB single-Buffer ceiling outright; and
//   - gave no way to enforce a cap before the bytes were already resident.
//
// This streams the busboy file part straight into an S3 Multipart Upload, holding
// at most ONE part-sized buffer in memory at a time, and enforces a hard byte cap:
// on breach it aborts the multipart (leaving NO half-written object) and throws
// FileTooLargeError so the route can answer a clean 413.

import type { Readable } from "stream";
import {
  S3Client,
  CreateMultipartUploadCommand,
  UploadPartCommand,
  CompleteMultipartUploadCommand,
  AbortMultipartUploadCommand,
} from "@aws-sdk/client-s3";

// 16 MiB parts. S3 requires every part EXCEPT the last to be >= 5 MiB, so this is
// comfortably valid; the final part may be smaller (a small file is a single part).
export const UPLOAD_PART_SIZE = 16 * 1024 * 1024;

// How many parts may be uploading to S3 at once. Concurrency (plus NOT blocking the
// client read while a part uploads) is what keeps this close to the old single-PUT
// speed: proxy→S3 sending overlaps client→proxy receiving instead of running after it.
// Peak buffered memory ≈ UPLOAD_CONCURRENCY * UPLOAD_PART_SIZE (~64 MiB) — still bounded.
export const UPLOAD_CONCURRENCY = 4;

/** Thrown (after aborting the multipart) when the stream exceeds `maxBytes`. */
export class FileTooLargeError extends Error {
  readonly code = "FILE_TOO_LARGE";
  constructor(public readonly maxBytes: number) {
    super(`File exceeds the maximum upload size of ${maxBytes} bytes`);
    this.name = "FileTooLargeError";
  }
}

export interface StreamUploadOptions {
  bucket: string;
  key: string;
  contentType: string;
  /** Hard cap; the transfer aborts and throws FileTooLargeError once it is exceeded. */
  maxBytes: number;
}

/**
 * Stream `source` (a busboy file part) into S3 via a Multipart Upload with bounded
 * memory AND overlapped, concurrent part uploads. Returns the total bytes written.
 * Aborts the multipart on ANY failure or cap breach so a rejected upload never leaves
 * a partial object behind.
 */
export async function streamUploadToS3(
  s3: S3Client,
  source: Readable,
  o: StreamUploadOptions,
): Promise<number> {
  const create = await s3.send(
    new CreateMultipartUploadCommand({ Bucket: o.bucket, Key: o.key, ContentType: o.contentType }),
  );
  const uploadId = create.UploadId as string;
  const parts: Array<{ PartNumber: number; ETag: string }> = [];
  const inFlight = new Set<Promise<void>>();
  let partNumber = 0;
  let total = 0;
  let pending: Buffer[] = [];
  let pendingLen = 0;
  let firstError: unknown = null;

  const abort = async () => {
    try {
      await s3.send(
        new AbortMultipartUploadCommand({ Bucket: o.bucket, Key: o.key, UploadId: uploadId }),
      );
    } catch {
      /* best effort — an orphaned multipart is swept by the bucket lifecycle rule */
    }
  };

  // Start a part upload WITHOUT blocking the caller (so reading the next chunk overlaps
  // the S3 send). Errors are captured into `firstError` rather than rejecting, so the
  // in-flight promises can be awaited safely without unhandled rejections.
  const startPart = (body: Buffer) => {
    partNumber += 1;
    const num = partNumber;
    let p: Promise<void>;
    p = (async () => {
      try {
        const up = await s3.send(
          new UploadPartCommand({
            Bucket: o.bucket,
            Key: o.key,
            UploadId: uploadId,
            PartNumber: num,
            Body: body,
          }),
        );
        parts.push({ PartNumber: num, ETag: up.ETag as string });
      } catch (err) {
        if (firstError === null) firstError = err;
      } finally {
        inFlight.delete(p);
      }
    })();
    inFlight.add(p);
  };

  try {
    for await (const chunk of source) {
      if (firstError !== null) throw firstError;
      const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as Uint8Array);
      total += buf.length;
      if (total > o.maxBytes) throw new FileTooLargeError(o.maxBytes);
      pending.push(buf);
      pendingLen += buf.length;
      if (pendingLen >= UPLOAD_PART_SIZE) {
        const body = Buffer.concat(pending, pendingLen);
        pending = [];
        pendingLen = 0;
        // Bound concurrency: wait for a slot before starting another part.
        if (inFlight.size >= UPLOAD_CONCURRENCY) await Promise.race(inFlight);
        startPart(body);
      }
    }
    if (pendingLen > 0) startPart(Buffer.concat(pending, pendingLen)); // final (possibly small) part
    await Promise.all(inFlight);
    if (firstError !== null) throw firstError;
    if (parts.length === 0) {
      // A zero-byte upload cannot complete a multipart (needs >= 1 part). Treat it
      // as an error rather than leaving an orphaned multipart open.
      throw new Error("No file content received");
    }
    // Parts may have completed out of order under concurrency; S3 requires them sorted.
    parts.sort((a, b) => a.PartNumber - b.PartNumber);
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
    await abort();
    throw err;
  }
}
