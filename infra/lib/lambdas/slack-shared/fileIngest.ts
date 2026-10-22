// Slack → S3 file ingest orchestration. For each candidate: refresh metadata
// (files.info), size-gate by declared size (decideByCaps), then stream the
// authenticated download into S3 via the bounded multipart transfer
// (./multipart), enforcing actual-byte + deadline aborts. Returns canonical
// refs (s3://+CloudFront) for accepted files.

import { S3Client } from "@aws-sdk/client-s3";
import { sanitizeFilename, inputKey, decideByCaps, type FileCandidate, type CapConfig } from "./ingest";
import { filesInfo } from "./slackClient";
import { streamToS3 } from "./multipart";

export interface IngestedFile {
  name: string;
  bucket: string;
  key: string;
  s3Uri: string;
  url: string; // permanent CloudFront URL
  mimetype: string;
  size: number; // actual bytes streamed
}

export interface IngestResult {
  accepted: IngestedFile[];
  rejected: Array<{ name: string; reason: string }>;
}

export interface IngestOptions {
  botToken: string;
  region: string;
  bucket: string;
  artifactsDomain: string;
  sessionId: string;
  files: FileCandidate[];
  caps: CapConfig;
  /** Epoch ms by which transfers must finish; else abort (leaves agent-turn reserve). */
  deadlineMs: number;
}

export async function ingestSlackFiles(opts: IngestOptions): Promise<IngestResult> {
  const s3 = new S3Client({ region: opts.region });
  const accepted: IngestedFile[] = [];
  const rejected: Array<{ name: string; reason: string }> = [];

  // Refresh metadata (fresh URL + declared size/type) before deciding.
  const refreshed: Array<FileCandidate & { url: string }> = [];
  for (const f of opts.files) {
    try {
      const info = await filesInfo(opts.botToken, f.id);
      if (!info.url) {
        rejected.push({ name: f.name, reason: "no download URL" });
        continue;
      }
      refreshed.push({
        id: f.id,
        name: info.name || f.name,
        mimetype: info.mimetype || f.mimetype,
        size: info.size || f.size,
        url: info.url,
      });
    } catch (err) {
      rejected.push({ name: f.name, reason: `files.info failed: ${(err as Error).message}` });
    }
  }

  const decision = decideByCaps(refreshed, opts.caps);
  for (const r of decision.reject) rejected.push({ name: r.name, reason: r.reason });

  let aggActual = 0;
  for (const f of decision.accept as Array<FileCandidate & { url: string }>) {
    if (Date.now() > opts.deadlineMs) {
      rejected.push({ name: f.name, reason: "preparation deadline reached before transfer" });
      continue;
    }
    const key = inputKey(opts.sessionId, f.id, sanitizeFilename(f.name));
    try {
      const bytes = await streamToS3(s3, {
        bucket: opts.bucket,
        key,
        contentType: f.mimetype,
        url: f.url,
        botToken: opts.botToken,
        perFileCap: opts.caps.maxFileBytes,
        aggRemaining: opts.caps.maxAggregateBytes - aggActual,
        deadlineMs: opts.deadlineMs,
      });
      aggActual += bytes;
      accepted.push({
        name: f.name,
        bucket: opts.bucket,
        key,
        s3Uri: `s3://${opts.bucket}/${key}`,
        url: `https://${opts.artifactsDomain}/${key}`,
        mimetype: f.mimetype,
        size: bytes,
      });
    } catch (err) {
      rejected.push({ name: f.name, reason: `transfer failed: ${(err as Error).message}` });
    }
  }

  return { accepted, rejected };
}
