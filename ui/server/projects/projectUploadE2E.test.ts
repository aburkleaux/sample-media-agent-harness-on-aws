// OPT-IN REAL-AWS E2E for the Projects upload/registration/remove/chat-copy routes (Projects Phase B/C).
//
// Skipped unless E2E_PROJECTS=1. It runs the REAL proxy (`buildApp()`) in-process with DEV_BYPASS_AUTH
// (a unique throwaway actor per run) against STAGING — the projects table, the sessions table, and the
// Ingest bucket — with the Asset_Service served locally by `agent/scripts/local_asset_service.py`
// (LOCAL_ASSET_SERVICE_URL), which runs the real handler with your AWS credentials. Steps:
//
//   create a Project → capabilities → presigned uploads of a small PNG (single PUT, Content-Type signed),
//   a small text document (single PUT), and a 101 MiB generated file (multipart, 2 parts) → complete →
//   register all three → list + detail → chat-copy the image into a test session → abort a second
//   multipart → remove all three and assert the S3 objects (project + chat copy) are gone → delete the
//   Project. A `finally` block then deletes EVERYTHING the run created: every object VERSION under
//   projects/<pid>/ and of the chat copy (the bucket is versioned), stray multipart uploads, and every
//   DynamoDB item in PROJECT#<pid> and USER#<actor>.
//
// Run (two terminals, staging credentials for account 100000000002):
//
//   # 1) the local Asset_Service (dev-bypass accepted ONLY because of the explicit flag)
//   cd agent && AWS_REGION=us-west-2 PROJECTS_TABLE=mediaworks-harness-projects \
//     INGEST_BUCKET=mediaworks-harness-skills-store-100000000002-us-west-2 \
//     uv run python scripts/local_asset_service.py --dev-bypass
//
//   # 2) the E2E
//   cd ui && E2E_PROJECTS=1 AWS_REGION=us-west-2 PROJECTS_TABLE=mediaworks-harness-projects \
//     SESSIONS_TABLE=mediaworks-harness-sessions LOCAL_ASSET_SERVICE_URL=http://127.0.0.1:8099 \
//     npx vitest run server/projects/projectUploadE2E.test.ts
//
// Optional: E2E_INGEST_BUCKET overrides the bucket (default the staging skills/Ingest bucket).
import { randomUUID } from "node:crypto";
import http from "node:http";
import {
  AbortMultipartUploadCommand,
  DeleteObjectsCommand,
  HeadObjectCommand,
  ListMultipartUploadsCommand,
  ListObjectVersionsCommand,
  S3Client,
} from "@aws-sdk/client-s3";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { BatchWriteCommand, DynamoDBDocumentClient, QueryCommand } from "@aws-sdk/lib-dynamodb";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { closeServers, listenLoopback } from "../testing/loopback";

const ENABLED = process.env.E2E_PROJECTS === "1";
const REGION = process.env.AWS_REGION || "us-west-2";
const BUCKET = process.env.E2E_INGEST_BUCKET || "mediaworks-harness-skills-store-100000000002-us-west-2";
const PROJECTS_TABLE = process.env.PROJECTS_TABLE || "";
const SESSIONS_TABLE = process.env.SESSIONS_TABLE || "";
const LOCAL_ASSET_SERVICE_URL = process.env.LOCAL_ASSET_SERVICE_URL || "http://127.0.0.1:8099";
const ACTOR = `e2e-projects-${randomUUID()}`;
const SESSION_ID = `e2e-chat-${randomUUID()}`;
const MIB = 1024 * 1024;

// A valid 1x1 transparent PNG.
const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=",
  "base64",
);
const TEXT = Buffer.from("Projects E2E document: every file type is a Project_Asset.\n", "utf8");
const BIG_SIZE = 101 * MIB; // >= 100 MiB → multipart, 2 parts of 64 MiB + 37 MiB

const servers: http.Server[] = [];
let base = "";
const s3 = new S3Client({ region: REGION });
const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }));
const created = { pid: "", copyKeys: [] as string[] };
const log = (...a: unknown[]) => console.log("[projects-e2e]", ...a);

async function api(method: string, path: string, body?: unknown) {
  const res = await fetch(`${base}${path}`, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let json: any;
  try {
    json = text ? JSON.parse(text) : undefined;
  } catch {
    json = { raw: text.slice(0, 200) };
  }
  return { status: res.status, json };
}

async function exists(key: string): Promise<boolean> {
  try {
    await s3.send(new HeadObjectCommand({ Bucket: BUCKET, Key: key }));
    return true;
  } catch (e: any) {
    if (e?.$metadata?.httpStatusCode === 404 || e?.name === "NotFound") return false;
    throw e;
  }
}

async function waitFor<T>(fn: () => Promise<T>, ok: (v: T) => boolean, label: string, ms = 20_000): Promise<T> {
  const deadline = Date.now() + ms;
  let last: T = await fn();
  while (!ok(last) && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 1000));
    last = await fn();
  }
  if (!ok(last)) throw new Error(`timed out waiting for ${label}`);
  return last;
}

/** Delete every version + delete marker under `prefix` (the bucket is versioned). */
async function purgeVersions(prefix: string): Promise<number> {
  let n = 0;
  let keyMarker: string | undefined;
  let versionMarker: string | undefined;
  for (let page = 0; page < 50; page++) {
    const r = await s3.send(
      new ListObjectVersionsCommand({ Bucket: BUCKET, Prefix: prefix, KeyMarker: keyMarker, VersionIdMarker: versionMarker }),
    );
    const objs = [...(r.Versions ?? []), ...(r.DeleteMarkers ?? [])]
      .filter((v) => v.Key && v.Key.startsWith(prefix))
      .map((v) => ({ Key: v.Key!, VersionId: v.VersionId }));
    if (objs.length) {
      await s3.send(new DeleteObjectsCommand({ Bucket: BUCKET, Delete: { Objects: objs, Quiet: true } }));
      n += objs.length;
    }
    if (!r.IsTruncated) break;
    keyMarker = r.NextKeyMarker;
    versionMarker = r.NextVersionIdMarker;
  }
  return n;
}

async function deletePartition(pk: string): Promise<number> {
  let n = 0;
  let start: Record<string, unknown> | undefined;
  do {
    const r = await ddb.send(
      new QueryCommand({
        TableName: PROJECTS_TABLE,
        KeyConditionExpression: "PK = :pk",
        ExpressionAttributeValues: { ":pk": pk },
        ProjectionExpression: "PK, SK",
        ExclusiveStartKey: start,
      }),
    );
    const items = r.Items ?? [];
    for (let i = 0; i < items.length; i += 25) {
      await ddb.send(
        new BatchWriteCommand({
          RequestItems: { [PROJECTS_TABLE]: items.slice(i, i + 25).map((it) => ({ DeleteRequest: { Key: { PK: it.PK, SK: it.SK } } })) },
        }),
      );
      n += Math.min(25, items.length - i);
    }
    start = r.LastEvaluatedKey as Record<string, unknown> | undefined;
  } while (start);
  return n;
}

async function cleanup(): Promise<void> {
  if (!PROJECTS_TABLE) return;
  // Every Project the throwaway actor touched (the created one, plus a Personal_Project if one was made).
  const mirrors = await ddb.send(
    new QueryCommand({
      TableName: PROJECTS_TABLE,
      KeyConditionExpression: "PK = :pk",
      ExpressionAttributeValues: { ":pk": `USER#${ACTOR}` },
    }),
  );
  const pids = new Set<string>(created.pid ? [created.pid] : []);
  for (const it of mirrors.Items ?? []) {
    if (typeof it.SK === "string" && it.SK.startsWith("PROJECT#")) pids.add(it.SK.slice("PROJECT#".length));
    if (it.SK === "PERSONAL" && typeof it.projectId === "string") pids.add(it.projectId);
  }
  for (const pid of pids) {
    const prefix = `projects/${pid}/`;
    const ups = await s3.send(new ListMultipartUploadsCommand({ Bucket: BUCKET, Prefix: prefix }));
    for (const u of ups.Uploads ?? []) {
      await s3.send(new AbortMultipartUploadCommand({ Bucket: BUCKET, Key: u.Key!, UploadId: u.UploadId! }));
    }
    const versions = await purgeVersions(prefix);
    const rows = await deletePartition(`PROJECT#${pid}`);
    log(`cleanup ${pid}: ${versions} object versions, ${rows} table items`);
  }
  for (const key of created.copyKeys) log(`cleanup chat copy ${key}: ${await purgeVersions(key)} versions`);
  log(`cleanup USER#${ACTOR}: ${await deletePartition(`USER#${ACTOR}`)} table items`);
}

describe.skipIf(!ENABLED)("Projects uploads E2E (real AWS, staging)", () => {
  beforeAll(async () => {
    if (!PROJECTS_TABLE || !SESSIONS_TABLE) throw new Error("PROJECTS_TABLE and SESSIONS_TABLE are required");
    // The local Asset_Service must be up: an empty event answers a JSON refusal, not a socket error.
    const probe = await fetch(`${LOCAL_ASSET_SERVICE_URL}/invoke`, { method: "POST", body: "{}" }).catch(() => null);
    if (!probe || !probe.ok) throw new Error(`local_asset_service.py is not reachable at ${LOCAL_ASSET_SERVICE_URL}`);
    Object.assign(process.env, {
      AGENT_RUNTIME_ARN: "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/e2e-AAAAAAAAAA",
      DEV_BYPASS_AUTH: "true",
      LAB_DEFAULT_ACTOR_ID: ACTOR,
      AWS_REGION: REGION,
      PROJECTS_TABLE,
      SESSIONS_TABLE,
      UPLOAD_BUCKET: BUCKET,
      LOCAL_ASSET_SERVICE_URL,
      ARTIFACTS_DOMAIN: process.env.ARTIFACTS_DOMAIN ?? "",
    });
    delete process.env.ASSET_SERVICE_FN;
    delete process.env.PROJECTS_DDB_ENDPOINT;
    delete process.env.PROJECTS_STORE;
    const { buildApp } = await import("../app");
    base = await listenLoopback(buildApp(), servers);
    log(`actor=${ACTOR} bucket=${BUCKET} table=${PROJECTS_TABLE}`);
  }, 60_000);

  afterAll(async () => {
    try {
      await cleanup();
    } finally {
      await closeServers(servers);
    }
  }, 300_000);

  it("uploads (single + multipart), registers, lists, chat-copies, removes, and deletes", async () => {
    // --- create a Project ---
    const name = `E2E uploads ${Date.now()}`;
    const proj = await api("POST", "/projects", { name });
    expect(proj.status, JSON.stringify(proj.json)).toBe(201);
    const pid = proj.json.project.projectId as string;
    created.pid = pid;
    log(`project ${pid}`);

    // --- capabilities ---
    const caps = await api("GET", `/projects/${pid}/ingestion/capabilities`);
    expect(caps.status).toBe(200);
    expect(caps.json).toEqual({
      maxFilesPerBatch: 20,
      limits: { video: 5368709120, audio: 5368709120, image: 52428800, document: 524288000 },
      multipartThresholdBytes: 104857600,
      partSizeBytes: 67108864,
    });

    // --- presigned upload grants ---
    const files = [
      { name: "e2e pixel.png", size: PNG.length, contentType: "image/png" },
      { name: "e2e notes.txt", size: TEXT.length, contentType: "text/plain" },
      { name: "e2e-big.bin", size: BIG_SIZE, contentType: "application/octet-stream" },
    ];
    const grants = await api("POST", `/projects/${pid}/uploads`, { files });
    expect(grants.status, JSON.stringify(grants.json)).toBe(200);
    expect(grants.json.errors).toEqual({});
    const [img, doc, big] = grants.json.uploads;
    expect(img).toMatchObject({ index: 0, mode: "single", headers: { "Content-Type": "image/png" } });
    expect(img.assetId).toMatch(/^image:/);
    expect(img.key).toBe(`projects/${pid}/${img.assetId}/e2e_pixel.png`);
    expect(doc).toMatchObject({ index: 1, mode: "single" });
    expect(doc.assetId).toMatch(/^document:/);
    expect(big).toMatchObject({ index: 2, mode: "multipart", partSize: 64 * MIB });
    expect(big.assetId).toMatch(/^document:/);
    expect(big.parts.map((p: any) => p.partNumber)).toEqual([1, 2]);

    // Content-Type is signed: a PUT declaring another type is refused by S3.
    const wrongType = await fetch(img.url, { method: "PUT", headers: { "Content-Type": "image/jpeg" }, body: PNG });
    expect(wrongType.status).toBe(403);

    for (const [g, bytes] of [[img, PNG], [doc, TEXT]] as const) {
      const put = await fetch(g.url, { method: "PUT", headers: g.headers, body: bytes });
      expect(put.status, `PUT ${g.key}`).toBe(200);
    }
    // Multipart: PUT each part to its presigned URL, then complete with the ETags.
    const bigBytes = Buffer.alloc(BIG_SIZE, 0x5a);
    const etags: { partNumber: number; etag: string }[] = [];
    const t0 = Date.now();
    for (const p of big.parts) {
      const start = (p.partNumber - 1) * big.partSize;
      const chunk = bigBytes.subarray(start, Math.min(start + big.partSize, BIG_SIZE));
      const r = await fetch(p.url, { method: "PUT", body: chunk });
      expect(r.status, `part ${p.partNumber}`).toBe(200);
      etags.push({ partNumber: p.partNumber, etag: r.headers.get("etag") ?? "" });
    }
    log(`multipart parts uploaded in ${Date.now() - t0} ms`);
    // Unbound uploadId (bound to another asset id) is refused before completing anything.
    const wrongAsset = await api("POST", `/projects/${pid}/uploads/${doc.assetId}/complete`, { uploadId: big.uploadId, parts: etags });
    expect(wrongAsset.status).toBe(404);
    const done = await api("POST", `/projects/${pid}/uploads/${big.assetId}/complete`, { uploadId: big.uploadId, parts: etags.reverse() });
    expect(done.status, JSON.stringify(done.json)).toBe(200);
    expect(done.json).toEqual({ ok: true });

    // An id with no uploaded object is a 409 object_missing.
    const missing = await api("POST", `/projects/${pid}/assets`, {
      assetId: `document:${randomUUID()}`, fileName: "nope.txt", contentType: "text/plain", size: 3,
    });
    expect(missing.status).toBe(409);
    expect(missing.json.error.code).toBe("object_missing");

    // --- register all three ---
    const views: Record<string, any> = {};
    for (const [g, f] of [[img, files[0]], [doc, files[1]], [big, files[2]]] as const) {
      const r = await api("POST", `/projects/${pid}/assets`, { assetId: g.assetId, fileName: f.name, contentType: f.contentType, size: f.size });
      expect(r.status, JSON.stringify(r.json)).toBe(200);
      expect(r.json).toMatchObject({ assetId: g.assetId, displayName: f.name, status: "queued", sourceSize: f.size, contentType: f.contentType });
      expect(r.json.sourceUri).toBeUndefined();
      views[g.assetId] = r.json;
    }
    expect(views[img.assetId].kind).toBe("image");
    expect(views[doc.assetId].kind).toBe("document");
    expect(views[big.assetId].kind).toBe("document");
    // Size mismatch is refused (the real object is PNG.length bytes), even for an already-registered id.
    const mismatch = await api("POST", `/projects/${pid}/assets`, {
      assetId: img.assetId, fileName: files[0].name, contentType: "image/png", size: PNG.length + 1,
    });
    expect(mismatch.status).toBe(409);
    expect(mismatch.json.error.code).toBe("size_mismatch");
    // Re-registering the same upload is idempotent: the same asset comes back.
    const again = await api("POST", `/projects/${pid}/assets`, {
      assetId: img.assetId, fileName: files[0].name, contentType: "image/png", size: PNG.length,
    });
    expect(again.status).toBe(200);
    expect(again.json.displayName).toBe("e2e pixel.png");

    // --- list (GSI, eventually consistent) + detail ---
    const listed = await waitFor(
      () => api("GET", `/projects/${pid}/assets`),
      (r) => r.status === 200 && r.json.assets.length === 3,
      "3 listed assets",
    );
    expect(new Set(listed.json.assets.map((a: any) => a.assetId))).toEqual(new Set([img.assetId, doc.assetId, big.assetId]));
    const detail = await api("GET", `/projects/${pid}/assets/${encodeURIComponent(doc.assetId)}`);
    expect(detail.status).toBe(200);
    expect(detail.json).toMatchObject({ assetId: doc.assetId, kind: "document", status: "queued" });

    // --- chat copy into a new test session (no session row: a new chat) ---
    const copy = await api("POST", `/projects/${pid}/assets/${encodeURIComponent(img.assetId)}/chat-copy`, { sessionId: SESSION_ID });
    expect(copy.status, JSON.stringify(copy.json)).toBe(200);
    expect(copy.json.key).toMatch(new RegExp(`^artifacts/${SESSION_ID}/input/\\d+-e2e_pixel\\.png$`));
    expect(copy.json).toMatchObject({ name: "e2e pixel.png", size: PNG.length, contentType: "image/png" });
    expect(typeof copy.json.url).toBe("string");
    created.copyKeys.push(copy.json.key);
    expect(await exists(copy.json.key)).toBe(true);

    // --- abort a second multipart (idempotent) ---
    const second = await api("POST", `/projects/${pid}/uploads`, { files: [{ name: "abort-me.bin", size: 150 * MIB }] });
    const g2 = second.json.uploads[0];
    expect(g2.mode).toBe("multipart");
    expect((await api("POST", `/projects/${pid}/uploads/${g2.assetId}/abort`, { uploadId: g2.uploadId })).json).toEqual({ ok: true });
    expect((await api("POST", `/projects/${pid}/uploads/${g2.assetId}/abort`, { uploadId: g2.uploadId })).json).toEqual({ ok: true });
    const left = await s3.send(new ListMultipartUploadsCommand({ Bucket: BUCKET, Prefix: `projects/${pid}/${g2.assetId}/` }));
    expect(left.Uploads ?? []).toEqual([]);

    // --- remove all three: project objects and the chat copy are gone ---
    for (const g of [img, doc, big]) expect(await exists(g.key), g.key).toBe(true);
    for (const g of [img, doc, big]) {
      const r = await api("POST", `/projects/${pid}/assets/${encodeURIComponent(g.assetId)}/remove`);
      expect(r.status, JSON.stringify(r.json)).toBe(200);
      expect(r.json).toEqual({ ok: true });
    }
    for (const g of [img, doc, big]) expect(await exists(g.key), `${g.key} deleted`).toBe(false);
    expect(await exists(copy.json.key), "chat copy deleted").toBe(false);
    // Idempotent, and the removed asset reads `removed` but is excluded from the list.
    expect((await api("POST", `/projects/${pid}/assets/${encodeURIComponent(img.assetId)}/remove`)).status).toBe(200);
    expect((await api("GET", `/projects/${pid}/assets/${encodeURIComponent(img.assetId)}`)).json.status).toBe("removed");
    await waitFor(() => api("GET", `/projects/${pid}/assets`), (r) => r.status === 200 && r.json.assets.length === 0, "an empty list");
    // A removed asset can no longer be copied into a chat.
    expect((await api("POST", `/projects/${pid}/assets/${encodeURIComponent(img.assetId)}/chat-copy`, { sessionId: SESSION_ID })).status).toBe(404);

    // --- delete the Project ---
    const del = await api("DELETE", `/projects/${pid}`, { confirmName: name });
    expect(del.status, JSON.stringify(del.json)).toBe(200);
    expect((await api("GET", `/projects/${pid}/ingestion/capabilities`)).status).toBe(404);
  }, 900_000);
});
