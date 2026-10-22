// Shared Slack Web API client for the worker + notifier Lambdas.
// - slackCall: chat.postMessage / chat.update (JSON)
// - headArtifact: size + content-type (S3 HeadObject for our artifacts, HTTP HEAD otherwise)
// - uploadFromUrl: native file upload (files.getUploadURLExternal → PUT → completeUploadExternal)
// Bot token stays in Lambda process memory. Uses global fetch (Node 22).

import { s3TargetFor, headObjectByKey, getObjectBytes, type ArtifactSource } from "./artifactS3";
export type { ArtifactSource } from "./artifactS3";

const SLACK_API = "https://slack.com/api";

export async function slackCall(
  botToken: string,
  method: string,
  payload: Record<string, unknown>,
): Promise<any> {
  const res = await fetch(`${SLACK_API}/${method}`, {
    method: "POST",
    headers: { authorization: `Bearer ${botToken}`, "content-type": "application/json; charset=utf-8" },
    body: JSON.stringify(payload),
  });
  const json: any = await res.json();
  if (!json.ok) throw new Error(`slack ${method} failed: ${json.error ?? "unknown"}`);
  return json;
}

export interface ArtifactHead {
  sizeBytes: number;
  contentType: string;
  filename: string;
}

/**
 * Size + content-type for an artifact; filename from the path. One of OUR artifacts-CDN URLs is
 * read by key from S3 (HeadObject) when `src` names a client + bucket — the CDN may be private, so
 * its URL is an identifier, not a fetchable address. Any other URL is HEADed over HTTP as before.
 */
export async function headArtifact(url: string, src?: ArtifactSource): Promise<ArtifactHead> {
  const target = s3TargetFor(url, src);
  if (target) return headObjectByKey(src!.s3!, target.bucket, target.key);
  const res = await fetch(url, { method: "HEAD" });
  const len = Number(res.headers.get("content-length") ?? "0");
  const ct = res.headers.get("content-type") ?? "application/octet-stream";
  const path = new URL(url).pathname;
  const filename = decodeURIComponent(path.split("/").pop() || "file");
  return { sizeBytes: Number.isFinite(len) ? len : 0, contentType: ct, filename };
}

/** The artifact's bytes: S3 GetObject by key for our artifacts-CDN URLs (see headArtifact), else HTTP GET. */
export async function fetchArtifactBytes(url: string, src?: ArtifactSource): Promise<Buffer> {
  const target = s3TargetFor(url, src);
  if (target) return getObjectBytes(src!.s3!, target.bucket, target.key);
  const artRes = await fetch(url);
  if (!artRes.ok) throw new Error(`fetch artifact ${artRes.status}`);
  return Buffer.from(await artRes.arrayBuffer());
}

/**
 * Native-upload an artifact (read from `url`, by S3 key when it is one of ours — see headArtifact)
 * into a Slack thread. Caller must have already size-gated via shouldUploadNatively. Buffers the
 * bytes (bounded by the cap) — no streaming needed for <= cap files.
 * Returns true on success; throws on Slack API failure.
 */
export async function uploadFromUrl(
  botToken: string,
  opts: { channel: string; threadTs: string; url: string; filename: string; comment?: string },
  src?: ArtifactSource,
): Promise<boolean> {
  // Fetch the artifact bytes.
  const bytes = await fetchArtifactBytes(opts.url, src);

  // 1) reserve an upload URL
  const form = new URLSearchParams({ filename: opts.filename, length: String(bytes.length) });
  const up = await fetch(`${SLACK_API}/files.getUploadURLExternal`, {
    method: "POST",
    headers: { authorization: `Bearer ${botToken}`, "content-type": "application/x-www-form-urlencoded" },
    body: form.toString(),
  });
  const upJson: any = await up.json();
  if (!upJson.ok) throw new Error(`getUploadURLExternal failed: ${upJson.error ?? "unknown"}`);

  // 2) PUT the bytes to the reserved URL
  const putRes = await fetch(upJson.upload_url, { method: "POST", body: bytes });
  if (!putRes.ok) throw new Error(`upload PUT ${putRes.status}`);

  // 3) complete + share into the thread
  const complete = await fetch(`${SLACK_API}/files.completeUploadExternal`, {
    method: "POST",
    headers: { authorization: `Bearer ${botToken}`, "content-type": "application/json; charset=utf-8" },
    body: JSON.stringify({
      files: [{ id: upJson.file_id, title: opts.filename }],
      channel_id: opts.channel,
      thread_ts: opts.threadTs,
      ...(opts.comment ? { initial_comment: opts.comment } : {}),
    }),
  });
  const compJson: any = await complete.json();
  if (!compJson.ok) throw new Error(`completeUploadExternal failed: ${compJson.error ?? "unknown"}`);
  return true;
}

export interface SlackFileInfo {
  url: string; // authenticated download URL (url_private_download)
  size: number;
  mimetype: string;
  name: string;
}

/** Fetch fresh file metadata + an authenticated download URL. Needs files:read. */
export async function filesInfo(botToken: string, fileId: string): Promise<SlackFileInfo> {
  const res = await fetch(`${SLACK_API}/files.info?file=${encodeURIComponent(fileId)}`, {
    headers: { authorization: `Bearer ${botToken}` },
  });
  const json: any = await res.json();
  if (!json.ok) throw new Error(`files.info failed: ${json.error ?? "unknown"}`);
  const f = json.file ?? {};
  return {
    url: f.url_private_download || f.url_private || "",
    size: Number(f.size ?? 0),
    mimetype: f.mimetype ?? "application/octet-stream",
    name: f.name ?? "file",
  };
}
