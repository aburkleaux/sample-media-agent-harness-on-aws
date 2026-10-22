// Read our own artifacts by S3 KEY instead of over the artifacts CDN.
//
// An artifacts-CDN URL is a stable identifier, not a fetchable address: with `artifactsCdnPrivate`
// the CDN answers 403 to everything. The worker + notifier therefore HEAD/GET the object directly in
// the skills bucket (execution role: s3:GetObject on artifacts/*). Non-artifact URLs keep using
// plain HTTP (see slackClient.ts).

import { GetObjectCommand, HeadObjectCommand } from "@aws-sdk/client-s3";
import { artifactKeyFromUrl } from "./notify";

/** Minimal S3 client surface (the real S3Client, or a test fake). */
export interface S3Like {
  send(command: any): Promise<any>;
}

/**
 * Where to read artifacts from. When `s3` + `bucket` are set and a URL is one of our artifacts-CDN
 * URLs, reads go to S3 by key; otherwise the caller falls back to HTTP.
 */
export interface ArtifactSource {
  s3?: S3Like;
  bucket?: string;
  artifactsDomain?: string;
}

/** The (bucket, key) to read `url` from, or null when it must be fetched over HTTP. */
export function s3TargetFor(url: string, src: ArtifactSource | undefined): { bucket: string; key: string } | null {
  if (!src?.s3 || !src.bucket || !src.artifactsDomain) return null;
  const key = artifactKeyFromUrl(url, src.artifactsDomain);
  return key ? { bucket: src.bucket, key } : null;
}

export async function headObjectByKey(
  s3: S3Like,
  bucket: string,
  key: string,
): Promise<{ sizeBytes: number; contentType: string; filename: string }> {
  const head = await s3.send(new HeadObjectCommand({ Bucket: bucket, Key: key }));
  const len = Number(head?.ContentLength ?? 0);
  return {
    sizeBytes: Number.isFinite(len) ? len : 0,
    contentType: head?.ContentType || "application/octet-stream",
    filename: key.split("/").pop() || "file",
  };
}

export async function getObjectBytes(s3: S3Like, bucket: string, key: string): Promise<Buffer> {
  const obj = await s3.send(new GetObjectCommand({ Bucket: bucket, Key: key }));
  if (!obj?.Body) return Buffer.alloc(0);
  return Buffer.from(await obj.Body.transformToByteArray());
}
